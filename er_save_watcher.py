#!/usr/bin/env python3
r"""
Multi-Game .sl2 Save File Watcher + Auto-Restore + On-Screen Overlay.

Watches a FromSoftware .sl2 save, snapshots it into numbered
"<stem> - Kopie (N).sl2" files while the state is "clean" (health > 0 and
souls > 0), and restores the latest clean snapshot after a death. Existing
Kopie files are never modified or deleted -- only new ones are added, and
only watcher-created snapshots beyond KOPIE_RETENTION_COUNT are pruned.

Games (-g/--game): er (Elden Ring, ER0000.sl2), dsr (Dark Souls
Remastered, DRAKS0005.sl2), ds3 (Dark Souls III, DS30000.sl2). All three
are BND4 containers; health/souls are read per game by read_vitals_er/
dsr/ds3 (see those functions and the constants/comments above each for
the format details and credits to the reverse-engineering projects we
relied on). dsr/ds3 are AES-encrypted and need the 'cryptography' package
(pip install cryptography); er is unencrypted and needs nothing extra.

Active-character detection: a .sl2 holds up to 10 characters but only the
one being played is rewritten on save, so the active slot is the one whose
BND4 entry checksum changes between saves (entry_fingerprints/changed_slot).
Falls back to the first occupied slot until a save is observed; override
with -s/--slot.

Death-restore gate: a restore fires only once the save has been unclean
AND quiet (not written) for DEATH_RESTORE_DELAY_SECONDS -- i.e. you've quit
to the main menu. A restore made while the game is still running just gets
overwritten by its next autosave; one made at the menu sticks (the game
reloads from disk on Continue). Restores are verified with copy_and_verify
(MD5 compare + retry).

Usage (Windows, Python 3 installed):
    python er_save_watcher.py [-g {er,dsr,ds3}] [-s SLOT] [save_dir]

    save_dir holds the save file; if omitted it's auto-detected (see
    GAME_PROFILES) when there's exactly one candidate subfolder.

The overlay is always-on-top and works over Borderless Windowed mode (true
exclusive Fullscreen can paint over it -- a Windows/DirectX limitation).
Drag its title bar to move it; click "x" to close it (the watcher keeps
running). A full log is written to save_changes.log next to the program.
"""

import argparse
import hashlib
import os
import re
import shutil
import struct
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime

try:
    import winsound  # Windows-only stdlib module
except ImportError:
    winsound = None

# ---- Configuration ---------------------------------------------------

# sys.executable is the .exe itself when frozen by PyInstaller; __file__ would
# otherwise resolve to a temp extraction folder that's deleted on exit.
_APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_APP_DIR, "save_changes.log")
POLL_INTERVAL_SECONDS = 1.0
MAX_EVENTS_SHOWN = 8
DEATH_RESTORE_DELAY_SECONDS = 15
DEATH_RESTORE_ESCALATION_WINDOW_SECONDS = 60  # DSR only; see _restore_after_death()
RESTORE_VERIFY_MAX_ATTEMPTS = 5
KOPIE_RETENTION_COUNT = 30  # how many watcher-created snapshots to keep before pruning the oldest; see _prune_old_kopies()
ENABLE_DEATH_RESTORE = True

# ---- Elden Ring specifics ----------------------------------------------

# ER's .sl2 is a BND4 container too (just unencrypted), so per-character slots are reachable the same way as
# DSR/DS3: a slot's data begins ER_ENTRY_DATA_SKIP bytes into its BND4 entry's data_offset (entry[0] is at
# 0x300, so slot 0 data is at 0x310 -- which matches the old hardcoded constant). The 16-byte skip is the
# per-slot checksum region (the same place the encrypted games put their AES IV).
ER_ENTRY_DATA_SKIP = 16
ER_GA_ITEM_COUNT = 0x1400  # number of GaItem slots preceding PlayerGameData, per ER-Save-Editor's SaveSlot::read()
ER_HEALTH_REL_OFFSET = 8  # byte offset of `health` within PlayerGameData, per its struct field order
ER_SOULS_REL_OFFSET = 100  # byte offset of `souls` (= runes) within PlayerGameData, per its struct field order
ER_READ_PREFIX_SIZE = 0x40000  # comfortably covers the header + worst-case ga_items array + PlayerGameData header

# ---- Dark Souls Remastered specifics ------------------------------------

DSR_AES_KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")
DSR_BND4_HEADER_LEN = 64
DSR_BND4_ENTRY_HEADER_LEN = 32
DSR_BND4_ENTRY_MAGIC = b"\x50\x00\x00\x00\xff\xff\xff\xff"
DSR_OCCUPANCY_REL_OFFSET = 176  # occupancy byte table within decrypted entry #10
DSR_HEALTH_REL_OFFSET = 96  # verified against two real characters' current/max HP
DSR_SOULS_REL_OFFSET = 224  # current (spendable) souls -- verified against a live in-game value of 8000.
DSR_SOULS_MEMORY_REL_OFFSET = 228  # lifetime cumulative souls; monotonically non-decreasing, never resets on death/spending.
# 228 (souls_givifier's documented "set both 224 and 228" target) was used here originally and seemed to verify
# correctly at the time -- but that was a coincidence: souls_memory only diverges from current souls once you
# actually spend or lose some, which hadn't happened yet in those earlier checks. Using souls_memory for death
# detection meant souls never read 0 after a death (it only ever grows), so the restore never fired.

# ---- Dark Souls III specifics --------------------------------------------

# Same BND4 container/entry constants as DSR (DSR_BND4_HEADER_LEN etc. are reused directly -- this is a
# generic FromSoftware container format, not DSR-specific), but its own AES key and occupancy-table offset.
# Credit: AES key from jtesta/souls_givifier (cross-checked against alfizari/Dark-Souls-3-Save-Editor-PS4-PC).
#
# An earlier version of this code computed health/souls via alfizari's gaprint()/Item.from_bytes() item-array-
# walk technique (offsets relative to the end of that array). That walk had a small but real drift (~4 bytes,
# likely because this game version's empty item slots are marked 0xFFFFFFFF rather than the 0x00000000 the
# reference assumed) which silently shifted every downstream field over by one: what it reported as "health"
# was actually FP, and "souls" was actually the never-resetting SOULS MEMORY field -- the exact same category
# of bug DSR hit earlier with its own souls/souls_memory mixup, caught here only because a live in-game value
# (souls=0 right after death) didn't match what was being read. Switched instead to the technique
# souls_givifier.py itself uses for DS3: locate the character's name (auto-extracted from entry #10's
# occupancy table, not user-supplied) within the decrypted character slot, then read fixed distances from
# THAT position -- see read_vitals_ds3(). Verified empirically against a live character (matched the in-game
# HP=550 and souls=700 exactly).
#
# Health is a 3-int cluster [current, max, base] at name_pos -112/-108/-104 (same order as Elden Ring's
# PlayerGameData). We read CURRENT at -112 (the one that drops to 0 on death); -104 (base max HP) was used
# at first and is wrong for death detection -- it never goes to 0. (A live full-HP/rested character reads all
# three equal, e.g. 550/550/550, which is why the distinction wasn't obvious until checked against a damaged
# state.) FP and Stamina follow as their own triplets just after.
DS3_AES_KEY = bytes.fromhex("fd464d695e69a39a10e319a7ace8b7fa")
DS3_OCCUPANCY_REL_OFFSET = 4244  # occupancy byte table within decrypted entry #10
DS3_NAME_TABLE_REL_OFFSET = 4254  # start of the per-slot name table within decrypted entry #10, right after the occupancy bytes
DS3_NAME_TABLE_STRIDE = 554  # bytes per slot within that name table
DS3_NAME_MAX_LEN = 16  # characters (UTF-16, so this many *2 bytes per slot's name field)
DS3_HEALTH_NAME_REL_OFFSET = -112  # CURRENT hp (drops to 0 on death), relative to the character name's position in the decrypted slot
DS3_SOULS_NAME_REL_OFFSET = -20  # current (spendable) souls; -16 from the same anchor is SOULS MEMORY (avoid, see above)

# ---- Per-game profiles ---------------------------------------------------


def _find_default_subfolder_dir(base):
    """Shared helper: returns base/<the one hex-or-digit-named subfolder>, or None if there's zero or more than one."""
    if not os.path.isdir(base):
        return None
    candidates = [
        d for d in os.listdir(base)
        if d and all(c in "0123456789abcdefABCDEF" for c in d) and os.path.isdir(os.path.join(base, d))
    ]
    if len(candidates) == 1:
        return os.path.join(base, candidates[0])
    return None


def _find_default_save_dir_er():
    """EldenRing saves live at %APPDATA%\\EldenRing\\<SteamID64>\\."""
    appdata = os.environ.get("APPDATA")
    return _find_default_subfolder_dir(os.path.join(appdata, "EldenRing")) if appdata else None


def _find_default_save_dir_dsr():
    """Dark Souls Remastered saves live at <Documents>\\NBGI\\DARK SOULS REMASTERED\\<SteamID64>\\."""
    documents = os.path.join(os.path.expanduser("~"), "Documents")
    return _find_default_subfolder_dir(os.path.join(documents, "NBGI", "DARK SOULS REMASTERED"))


def _find_default_save_dir_ds3():
    """Dark Souls III saves live at %APPDATA%\\DarkSoulsIII\\<id>\\ (not always a plain decimal SteamID64)."""
    appdata = os.environ.get("APPDATA")
    return _find_default_subfolder_dir(os.path.join(appdata, "DarkSoulsIII")) if appdata else None


GAME_PROFILES = {
    "er": {
        "save_filename": "ER0000.sl2",
        "find_default_save_dir": _find_default_save_dir_er,
    },
    "dsr": {
        "save_filename": "DRAKS0005.sl2",
        "find_default_save_dir": _find_default_save_dir_dsr,
    },
    "ds3": {
        "save_filename": "DS30000.sl2",
        "find_default_save_dir": _find_default_save_dir_ds3,
    },
}


def _find_player_game_data_start(buf):
    """
    Walks the variable-length ga_items array (each entry is 8, 16, or 25 bytes
    depending on item type) to find where PlayerGameData begins. There is no
    fixed offset for this -- it shifts whenever held/equipped items change.
    Mirrors GaItem::read() in ClayAmore/ER-Save-Editor's save_slot.rs.
    """
    pos = 4 + 4 + 0x18  # ver + map_id + unknown padding, relative to slot start
    for _ in range(ER_GA_ITEM_COUNT):
        item_id = struct.unpack_from("<I", buf, pos + 4)[0]
        pos += 8  # gaitem_handle + item_id
        high_nibble = item_id & 0xF0000000
        if item_id != 0 and high_nibble == 0:
            pos += 4 + 4 + 4 + 1  # weapon-type GaItem
        elif item_id != 0 and high_nibble == 0x10000000:
            pos += 4 + 4  # armor-type GaItem
    return pos


def read_vitals_er(path, slot=None):
    """
    Returns (health, souls) for the Elden Ring save at path, or (None, None) on
    any read failure. slot None defaults to 0 (ER's occupancy table offset isn't
    reverse-engineered here, so we can't list occupied slots -- but active-slot
    detection picks the right one once a save is observed regardless). Reads only
    the BND4 header plus a 256KB window from the chosen slot, not the whole ~29MB.
    """
    if slot is None:
        slot = 0
    try:
        with open(path, "rb") as f:
            header = f.read(DSR_BND4_HEADER_LEN + DSR_BND4_ENTRY_HEADER_LEN * 16)
            if header[0:4] != b"BND4":
                return None, None
            pos = DSR_BND4_HEADER_LEN + DSR_BND4_ENTRY_HEADER_LEN * slot
            data_offset = struct.unpack_from("<i", header, pos + 16)[0]
            f.seek(data_offset + ER_ENTRY_DATA_SKIP)
            buf = f.read(ER_READ_PREFIX_SIZE)
    except OSError:
        return None, None
    try:
        player_game_data_start = _find_player_game_data_start(buf)
        health = struct.unpack_from("<I", buf, player_game_data_start + ER_HEALTH_REL_OFFSET)[0]
        souls = struct.unpack_from("<I", buf, player_game_data_start + ER_SOULS_REL_OFFSET)[0]
        return health, souls
    except struct.error:
        return None, None


def _bnd4_parse_entries(raw):
    """
    Parses a generic BND4 container header (used by DSR/DS2/DS3/ER alike) and
    returns a list of (size, data_offset) tuples, one per entry. Each entry's
    encrypted/plain payload starts 16 bytes into its data_offset (that first
    16 bytes is the AES IV for encrypted games, or just padding for ER).
    Raises ValueError if the BND4 magic or an entry's magic doesn't match.
    """
    if raw[0:4] != b"BND4":
        raise ValueError("not a BND4 file (bad magic)")
    num_entries = struct.unpack_from("<i", raw, 12)[0]
    entries = []
    for i in range(num_entries):
        pos = DSR_BND4_HEADER_LEN + DSR_BND4_ENTRY_HEADER_LEN * i
        header = raw[pos:pos + DSR_BND4_ENTRY_HEADER_LEN]
        if header[0:8] != DSR_BND4_ENTRY_MAGIC:
            raise ValueError(f"BND4 entry #{i} has an unexpected magic value")
        size = struct.unpack_from("<i", header, 8)[0]
        data_offset = struct.unpack_from("<i", header, 16)[0]
        entries.append((size, data_offset))
    return entries


def _bnd4_decrypt_entry(raw, size, data_offset, aes_key):
    """
    Decrypts one BND4 entry with AES-128-CBC (key is published/well-known,
    not a secret -- see jtesta/souls_givifier). The IV is the first 16 bytes
    at data_offset; the true (unpadded) length is a 4-byte int right after
    decryption starts, followed by that many bytes of actual save data.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # imported lazily: optional dep, only needed for encrypted-save games

    iv = raw[data_offset + 16:data_offset + 32]
    encrypted = raw[data_offset + 16:data_offset + size]
    decryptor = Cipher(algorithms.AES128(aes_key), modes.CBC(iv)).decryptor()
    decrypted = decryptor.update(encrypted) + decryptor.finalize()
    data_len = struct.unpack_from("<i", decrypted, 16)[0]
    return decrypted[20:20 + data_len]


def _bnd4_occupied_slots(raw, entries, aes_key, occupancy_rel_offset):
    """
    Entry #10 (the 11th) holds an occupancy table for the 10 character slots,
    one byte each, starting at occupancy_rel_offset within its decrypted data.
    Returns the list of occupied slot indices, lowest first.
    """
    size, data_offset = entries[10]
    occupancy_data = _bnd4_decrypt_entry(raw, size, data_offset, aes_key)
    slot_bytes = occupancy_data[occupancy_rel_offset:occupancy_rel_offset + 10]
    return [i for i in range(10) if slot_bytes[i:i + 1] != b"\x00"]


def entry_fingerprints(path):
    """
    For active-character detection across all three games (all BND4): returns
    {slot_index: checksum_bytes} for character entries 0-9, reading just the
    16-byte per-slot checksum at each entry's data_offset (cheap -- no full
    read or decryption). FromSoftware rewrites only the active character's
    entry on save, so the slot whose checksum changes between two saves is the
    one being played. Returns None on any failure.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(DSR_BND4_HEADER_LEN + DSR_BND4_ENTRY_HEADER_LEN * 16)
            if header[0:4] != b"BND4":
                return None
            num_entries = struct.unpack_from("<i", header, 12)[0]
            out = {}
            for i in range(min(num_entries, 10)):
                pos = DSR_BND4_HEADER_LEN + DSR_BND4_ENTRY_HEADER_LEN * i
                data_offset = struct.unpack_from("<i", header, pos + 16)[0]
                f.seek(data_offset)
                out[i] = f.read(16)
            return out
    except (OSError, struct.error):
        return None


def changed_slot(prev_fp, new_fp):
    """
    Given two {slot: checksum} maps, returns the single slot whose checksum
    changed (the active character on that save), or None if zero slots, more
    than one slot, or either map is missing changed -- only a lone change is
    unambiguous enough to act on.
    """
    if not prev_fp or not new_fp:
        return None
    changed = [s for s in new_fp if s in prev_fp and prev_fp[s] != new_fp[s]]
    return changed[0] if len(changed) == 1 else None


def read_vitals_dsr(path, slot=None):
    """Returns (health, souls) for the DSR save at path, or (None, None) on any read failure.
    slot None (or a slot that isn't occupied) falls back to the first occupied slot."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, None
    try:
        entries = _bnd4_parse_entries(raw)
        occupied = _bnd4_occupied_slots(raw, entries, DSR_AES_KEY, DSR_OCCUPANCY_REL_OFFSET)
        if not occupied:
            return None, None
        if slot is None or slot not in occupied:
            slot = occupied[0]
        size, data_offset = entries[slot]
        decrypted = _bnd4_decrypt_entry(raw, size, data_offset, DSR_AES_KEY)
        health = struct.unpack_from("<I", decrypted, DSR_HEALTH_REL_OFFSET)[0]
        souls = struct.unpack_from("<I", decrypted, DSR_SOULS_REL_OFFSET)[0]
        return health, souls
    except (ValueError, struct.error):
        return None, None
    except ImportError:
        return None, None


def _ds3_occupied_slots_and_names(raw, entries):
    """
    Entry #10 holds both the occupancy table (one byte per slot, at
    DS3_OCCUPANCY_REL_OFFSET) and a compact per-slot name table right after
    it (DS3_NAME_TABLE_REL_OFFSET, DS3_NAME_TABLE_STRIDE bytes apart, each a
    UTF-16 string up to DS3_NAME_MAX_LEN characters, null-terminated).
    Returns {slot_index: name} for every occupied slot.
    Mirrors unified_get_slot_occupancy() in jtesta/souls_givifier.
    """
    size, data_offset = entries[10]
    decrypted = _bnd4_decrypt_entry(raw, size, data_offset, DS3_AES_KEY)
    slot_bytes = decrypted[DS3_OCCUPANCY_REL_OFFSET:DS3_OCCUPANCY_REL_OFFSET + 10]
    out = {}
    for i in range(10):
        if slot_bytes[i:i + 1] == b"\x00":
            continue
        name_offset = DS3_NAME_TABLE_REL_OFFSET + DS3_NAME_TABLE_STRIDE * i
        name_bytes = decrypted[name_offset:name_offset + DS3_NAME_MAX_LEN * 2]
        null_pos = name_bytes.find(b"\x00\x00")
        if null_pos != -1:
            name_bytes = name_bytes[:null_pos + 1]
        out[i] = name_bytes.decode("utf-16")
    return out


def _ds3_encode_name(name):
    """
    Re-encodes a character name the way it appears in decrypted DS3 slot
    data: one null byte after each ASCII character (NOT real UTF-16 --
    Python's utf-16 encoder inserts a BOM and doesn't match). Mirrors
    encode_char_name() in jtesta/souls_givifier. Names with non-ASCII
    characters won't be found this way -- a known limitation upstream too.
    """
    out = b""
    for ch in name:
        out += ch.encode("ascii") + b"\x00"
    return out


def read_vitals_ds3(path, slot=None):
    """Returns (health, souls) for the DS3 save at path, or (None, None) on any read failure.
    slot None (or a slot that isn't occupied) falls back to the first occupied slot."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, None
    try:
        entries = _bnd4_parse_entries(raw)
        names = _ds3_occupied_slots_and_names(raw, entries)
        if not names:
            return None, None
        if slot is None or slot not in names:
            slot = min(names)
        name = names[slot]
        size, data_offset = entries[slot]
        decrypted = _bnd4_decrypt_entry(raw, size, data_offset, DS3_AES_KEY)
        name_pos = decrypted.find(_ds3_encode_name(name))
        if name_pos == -1:
            return None, None
        health = struct.unpack_from("<I", decrypted, name_pos + DS3_HEALTH_NAME_REL_OFFSET)[0]
        souls = struct.unpack_from("<I", decrypted, name_pos + DS3_SOULS_NAME_REL_OFFSET)[0]
        return health, souls
    except (ValueError, struct.error, UnicodeDecodeError, UnicodeEncodeError):
        return None, None
    except ImportError:
        return None, None


def is_clean_state(health, souls):
    """
    A snapshot/state is only trustworthy as a "good" checkpoint if the player
    is both alive (health > 0) AND has already reclaimed their runes
    (souls > 0). The gap right after respawning -- alive again, but runes
    still sitting unclaimed at the death location -- is excluded too, not
    just the moment of death itself. None readings are treated as "allow"
    since we can't determine the state and don't want to block on that.
    """
    if health is None or souls is None:
        return True
    return health != 0 and souls != 0


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_and_verify(source, dest, max_attempts=RESTORE_VERIFY_MAX_ATTEMPTS):
    """
    Copies source -> dest, then confirms the MD5s actually match -- a plain
    shutil.copy2() can silently leave dest out of sync with source if the
    game is touching either file at the same moment. Retries the whole copy
    (not just the comparison) up to max_attempts times if they don't match.
    Returns (True, None) on success, or (False, last_exception_or_None).
    """
    last_exc = None
    for _ in range(max_attempts):
        try:
            shutil.copy2(source, dest)
            if _md5_file(source) == _md5_file(dest):
                return True, None
        except OSError as exc:
            last_exc = exc
    return False, last_exc


def _kopie_name_re(save_stem):
    return re.compile(rf"^{re.escape(save_stem)} - Kopie(?: \((\d+)\))?\.sl2$")


def list_numbered_kopies(save_dir, save_stem):
    """Return [(number, path), ...] for files matching the Kopie naming scheme, newest (highest number) first."""
    name_re = _kopie_name_re(save_stem)
    found = []
    for name in os.listdir(save_dir):
        match = name_re.match(name)
        if match:
            number = int(match.group(1)) if match.group(1) else 1
            found.append((number, os.path.join(save_dir, name)))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return found


def next_kopie_path(save_dir, save_stem):
    existing = list_numbered_kopies(save_dir, save_stem)
    next_number = existing[0][0] + 1 if existing else 1
    name = f"{save_stem} - Kopie.sl2" if next_number == 1 else f"{save_stem} - Kopie ({next_number}).sl2"
    return os.path.join(save_dir, name)


def _watcher_state_file(save_stem):
    # Per-save-stem, so tracking two different games' baselines from the same _APP_DIR never collides.
    return os.path.join(_APP_DIR, f"watcher_state_{save_stem}.txt")


def load_or_init_kopie_baseline(save_dir, save_stem):
    """
    Returns the Kopie number at or below which files are pre-existing (manual
    backups predating the watcher, never touched) and above which they were
    created by the watcher (eligible for pruning). Persisted to disk on first
    ever run so this distinction survives process restarts -- without it,
    pruning would have no memory of which files it already created and would
    never catch up.
    """
    state_file = _watcher_state_file(save_stem)
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        pass
    existing = list_numbered_kopies(save_dir, save_stem)
    baseline = existing[0][0] if existing else 0
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            f.write(str(baseline))
    except OSError:
        pass
    return baseline

# ---- File watcher ------------------------------------------------------


class SaveWatcher:
    def __init__(self, path, save_stem, read_vitals_fn, on_change, on_snapshot, on_snapshot_failed,
                 on_death_restore, on_death_restore_failed, on_prune, on_prune_failed, on_active_slot,
                 use_adaptive_rewind=False, slot_override=None):
        self.path = path
        self.save_dir = os.path.dirname(path)
        self.save_stem = save_stem
        self.read_vitals = read_vitals_fn
        self.on_change = on_change
        self.on_snapshot = on_snapshot
        self.on_snapshot_failed = on_snapshot_failed
        self.on_death_restore = on_death_restore
        self.on_death_restore_failed = on_death_restore_failed
        self.on_prune = on_prune
        self.on_prune_failed = on_prune_failed
        self.on_active_slot = on_active_slot
        self.use_adaptive_rewind = use_adaptive_rewind
        self.slot_override = slot_override
        self._last_size = None
        self._last_mtime = None
        self._last_hash = None
        self._death_restore_pending = False
        self._death_restore_skip = 0  # adaptive rewind only: how many extra clean snapshots back to skip
        self._last_death_restore_monotonic = None  # adaptive rewind only: when the last death-restore happened
        self._kopie_baseline = load_or_init_kopie_baseline(self.save_dir, self.save_stem)
        # Which character slot to read vitals from. These games store up to 10 characters in one .sl2; only
        # the one being played is rewritten on save, so we detect it by which BND4 entry's checksum changes
        # (see entry_fingerprints/changed_slot). None until determined -> read_vitals falls back to the first
        # occupied slot. A fixed slot_override (CLI) disables auto-detection.
        self._active_slot = slot_override
        self._prev_fingerprints = None
        self._bootstrap_active_slot()
        self._stop = threading.Event()

    def _bootstrap_active_slot(self):
        # Determine the active slot up front (so the very first reads are correct) by diffing the two most
        # recent snapshots -- two consecutive saves of the active character differ only in that slot.
        if self.slot_override is not None:
            return
        kopies = list_numbered_kopies(self.save_dir, self.save_stem)
        if len(kopies) >= 2:
            cand = changed_slot(entry_fingerprints(kopies[1][1]), entry_fingerprints(kopies[0][1]))
            if cand is not None:
                self._active_slot = cand

    def _refresh_active_slot(self, path):
        if self.slot_override is not None:
            return
        fingerprints = entry_fingerprints(path)
        if fingerprints is None:
            return
        cand = changed_slot(self._prev_fingerprints, fingerprints)
        if cand is not None and cand != self._active_slot:
            self._active_slot = cand
            self.on_active_slot(cand)
        self._prev_fingerprints = fingerprints

    def _read_vitals(self, path):
        return self.read_vitals(path, self._active_slot)

    def _hash_file(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _poll_once(self):
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            return
        size, mtime = stat.st_size, stat.st_mtime
        if size == self._last_size and mtime == self._last_mtime:
            return
        try:
            digest = self._hash_file(self.path)
        except OSError:
            return  # file briefly locked by the game; retry next tick
        if digest == self._last_hash:
            self._last_size, self._last_mtime = size, mtime
            return
        delta = None if self._last_size is None else size - self._last_size
        self._last_size, self._last_mtime, self._last_hash = size, mtime, digest
        self._refresh_active_slot(self.path)  # re-detect active character before reading its vitals
        health, souls = self._read_vitals(self.path)
        self.on_change(size, delta, digest[:12], health, souls)
        if delta is None:  # don't snapshot the initial baseline read, only real changes
            return
        if is_clean_state(health, souls):
            self._snapshot_change()

    def _check_death(self):
        # Only restore once the save file has been UNCLEAN *and* quiet -- not written for
        # DEATH_RESTORE_DELAY_SECONDS. The quiet part is key: while you're still playing after a death the game
        # keeps rewriting the save with the (still-unclean) live state every few seconds, and any restore we
        # made would be instantly overwritten. The file only goes quiet once you've quit to the main menu, and
        # a restore done then sticks (the game reloads from disk on Continue). We read the file's actual mtime
        # each check (rather than a timer maintained elsewhere) so this is immune to poll-ordering races -- and
        # since our own restore write bumps the mtime, it also can't immediately re-fire on its own restore.
        try:
            seconds_since_write = time.time() - os.path.getmtime(self.path)
        except OSError:
            return
        health, souls = self._read_vitals(self.path)
        if health is None or souls is None:
            return
        if is_clean_state(health, souls):
            self._death_restore_pending = False
            return
        if self._death_restore_pending:
            return  # already attempted a restore for this unclean episode; wait for it to clear
        if seconds_since_write < DEATH_RESTORE_DELAY_SECONDS:
            return  # file written recently (you're still playing) -- wait for it to go quiet (main menu)
        self._death_restore_pending = True
        self._restore_after_death(health, souls)

    def _snapshot_change(self):
        try:
            dest = next_kopie_path(self.save_dir, self.save_stem)
            shutil.copy2(self.path, dest)  # new file only; never overwrites an existing Kopie file
        except OSError as exc:
            self.on_snapshot_failed(str(exc))
            return
        self.on_snapshot(os.path.basename(dest))
        self._prune_old_kopies()

    def _prune_old_kopies(self):
        # Re-derived from disk every time, filtered to numbers above the
        # persisted baseline, rather than an in-memory list -- that way
        # pruning still works correctly after the watcher restarts (it
        # doesn't survive game restarts, crashes, or a reboot), instead of
        # forgetting everything and needing KOPIE_RETENTION_COUNT fresh
        # snapshots before it can resume. Files at/below the baseline
        # (pre-existing manual backups) are never included here.
        own = sorted(
            (number, p) for number, p in list_numbered_kopies(self.save_dir, self.save_stem)
            if number > self._kopie_baseline
        )
        while len(own) > KOPIE_RETENTION_COUNT:
            _, oldest = own.pop(0)
            try:
                os.remove(oldest)
            except OSError as exc:
                self.on_prune_failed(str(exc))
                continue
            self.on_prune(os.path.basename(oldest))

    def _restore_after_death(self, trigger_health, trigger_souls):
        try:
            kopies = list_numbered_kopies(self.save_dir, self.save_stem)
        except OSError as exc:
            self.on_death_restore_failed(str(exc))
            return
        clean_candidates = []  # newest first; only ones that are a clean (alive + souls reclaimed) state
        for _, candidate in kopies:
            health, souls = self._read_vitals(candidate)
            if health is not None and souls is not None and health != 0 and souls != 0:
                clean_candidates.append(candidate)
        if not clean_candidates:
            self.on_death_restore_failed("no clean (health > 0 and souls > 0) snapshot found to restore from")
            return

        skip = 0
        if self.use_adaptive_rewind:
            # If the previous death-restore was recent, that restored snapshot apparently wasn't
            # safe either (another death followed quickly) -- skip one snapshot further back than
            # last time. Otherwise (first death in a while, e.g. a normal mid-fight death) start
            # fresh at the most recent clean snapshot, so ordinary deaths lose minimal progress.
            now = time.monotonic()
            if (self._last_death_restore_monotonic is not None
                    and now - self._last_death_restore_monotonic < DEATH_RESTORE_ESCALATION_WINDOW_SECONDS):
                self._death_restore_skip += 1
            else:
                self._death_restore_skip = 0
            skip = min(self._death_restore_skip, len(clean_candidates) - 1)

        source = clean_candidates[skip]
        ok, exc = copy_and_verify(source, self.path)  # one-way copy; source file is never renamed/moved/deleted
        if not ok:
            reason = str(exc) if exc else f"MD5 of restored file never matched source after {RESTORE_VERIFY_MAX_ATTEMPTS} attempts"
            self.on_death_restore_failed(reason)
            return
        stat = os.stat(self.path)
        self._last_size, self._last_mtime = stat.st_size, stat.st_mtime
        self._last_hash = self._hash_file(self.path)
        if self.use_adaptive_rewind:
            self._last_death_restore_monotonic = time.monotonic()
        self.on_death_restore(os.path.basename(source), trigger_health, trigger_souls, skip)

    def run(self):
        while not self._stop.is_set():
            if ENABLE_DEATH_RESTORE:
                self._check_death()  # polled every tick, independent of hash-based change detection
            self._poll_once()
            time.sleep(POLL_INTERVAL_SECONDS)

    def stop(self):
        self._stop.set()


# ---- Logging -------------------------------------------------------------


def _write_log(line):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_change(size, delta, short_hash, health, souls):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    delta_str = "first snapshot" if delta is None else f"{delta:+d} bytes"
    line = f"[{timestamp}] size={size} ({delta_str}) hash={short_hash} health={health} souls={souls}"
    _write_log(line)
    return f"{timestamp}  {delta_str}  #{short_hash}  health={health} souls={souls}"


def log_snapshot(name):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] snapshot saved as '{name}'"
    _write_log(line)
    return f"{timestamp}  snapshot saved as '{name}'"


def log_snapshot_failed(reason):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] SNAPSHOT FAILED: {reason}"
    _write_log(line)
    return f"{timestamp}  SNAPSHOT FAILED: {reason}"


def log_prune(name):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] pruned old snapshot '{name}' (retention: {KOPIE_RETENTION_COUNT})"
    _write_log(line)
    return f"{timestamp}  pruned '{name}'"


def log_prune_failed(reason):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] PRUNE FAILED: {reason}"
    _write_log(line)
    return f"{timestamp}  PRUNE FAILED: {reason}"


def log_death_restore(source_name, health, souls, skip):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    skip_str = f" (skipped {skip} more recent clean snapshot{'s' if skip != 1 else ''})" if skip else ""
    line = f"[{timestamp}] UNCLEAN STATE (health={health}, souls={souls}) -- RESTORED '{source_name}' -> live save{skip_str}"
    _write_log(line)
    return f"{timestamp}  RESTORED '{source_name}' (was health={health}, souls={souls}){skip_str}"


def log_death_restore_failed(reason):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] DEATH DETECTED but RESTORE FAILED: {reason}"
    _write_log(line)
    return f"{timestamp}  DIED but restore failed: {reason}"


def log_active_slot(slot):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] tracking active character slot {slot}"
    _write_log(line)
    return f"{timestamp}  active character: slot {slot}"


def play_snapshot_sound():
    if winsound is None:
        return
    try:
        winsound.Beep(440, 80)  # short single tone, distinct from the restore sound
    except RuntimeError:
        pass


def play_restore_sound():
    if winsound is None:
        return
    try:
        # Beep() drives a tone directly instead of relying on the Windows sound
        # scheme, which MessageBeep() does -- many systems mute/disable that.
        winsound.Beep(660, 150)
        winsound.Beep(880, 200)
    except RuntimeError:
        pass


# ---- Overlay ---------------------------------------------------------------


class Overlay:
    def __init__(self, save_filename):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.85)
        self.root.configure(bg="black")
        self.root.geometry("+40+40")

        header = tk.Frame(self.root, bg="#202020")
        header.pack(fill="x")
        tk.Label(
            header, text=f"{save_filename} Watcher", fg="white", bg="#202020",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=6, pady=2)
        tk.Button(
            header, text="x", fg="white", bg="#202020", bd=0,
            command=self.root.destroy, font=("Segoe UI", 9, "bold"),
        ).pack(side="right", padx=4)

        self.body = tk.Label(
            self.root, text="Waiting for changes...", fg="#33ff66", bg="black",
            justify="left", anchor="w", font=("Consolas", 9), padx=8, pady=6,
        )
        self.body.pack(fill="both", expand=True)

        for widget in (self.root, header, self.body):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._do_move)

        self.events = deque(maxlen=MAX_EVENTS_SHOWN)
        self._offset = (0, 0)

    def _start_move(self, event):
        self._offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _do_move(self, event):
        x = event.x_root - self._offset[0]
        y = event.y_root - self._offset[1]
        self.root.geometry(f"+{x}+{y}")

    def push_event(self, text):
        self.events.appendleft(text)
        self.body.config(text="\n".join(self.events))

    def schedule(self, callback, interval_ms=200):
        def tick():
            callback()
            self.root.after(interval_ms, tick)
        self.root.after(interval_ms, tick)

    def mainloop(self):
        self.root.mainloop()


# ---- Wiring -----------------------------------------------------------------


def parse_args():
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("-g", "--game", choices=sorted(GAME_PROFILES), default="er")
    pre_args, _ = pre_parser.parse_known_args()
    profile = GAME_PROFILES[pre_args.game]
    auto_detected = profile["find_default_save_dir"]()

    parser = argparse.ArgumentParser(description="FromSoftware .sl2 save watcher with auto-restore and on-screen overlay.")
    parser.add_argument(
        "-g", "--game", choices=sorted(GAME_PROFILES), default="er",
        help="Which game's save format to use (default: er). Determines the save filename and Kopie naming.",
    )
    parser.add_argument(
        "save_dir", nargs="?", default=auto_detected,
        help=f"Path to the save folder containing {profile['save_filename']}"
             + (f" (default, auto-detected: {auto_detected})" if auto_detected else
                " (required: couldn't auto-detect a unique save folder for this game)"),
    )
    parser.add_argument(
        "-s", "--slot", type=int, default=None,
        help="Force a specific character slot (0-9) instead of auto-detecting the active one. "
             "Normally unnecessary -- the watcher detects which character is being played by which "
             "save slot changes -- but useful to override if detection picks wrong.",
    )
    args = parser.parse_args()
    if args.save_dir is None:
        parser.error(
            "no save_dir given and couldn't auto-detect one for this game "
            "(either the expected base folder doesn't exist, or there's more than one SteamID subfolder there) -- "
            "pass the save folder path explicitly."
        )
    return args


def main():
    args = parse_args()
    profile = GAME_PROFILES[args.game]
    save_filename = profile["save_filename"]
    save_stem = save_filename[:-len(".sl2")]
    save_file = os.path.join(args.save_dir, save_filename)

    if args.game in ("dsr", "ds3"):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            print(
                f"ERROR: {args.game} support requires the 'cryptography' package, which isn't installed.\n"
                "Install it with:  pip install cryptography",
                file=sys.stderr,
            )
            sys.exit(1)
        read_vitals_fn = read_vitals_dsr if args.game == "dsr" else read_vitals_ds3
    else:
        read_vitals_fn = read_vitals_er

    overlay = Overlay(save_filename)
    pending = deque()
    pending_lock = threading.Lock()

    def on_change(size, delta, short_hash, health, souls):
        line = log_change(size, delta, short_hash, health, souls)
        with pending_lock:
            pending.append(line)

    def on_snapshot(name):
        line = log_snapshot(name)
        with pending_lock:
            pending.append(line)
        play_snapshot_sound()

    def on_snapshot_failed(reason):
        line = log_snapshot_failed(reason)
        with pending_lock:
            pending.append(line)

    def on_death_restore(source_name, health, souls, skip):
        line = log_death_restore(source_name, health, souls, skip)
        with pending_lock:
            pending.append(line)
        play_restore_sound()

    def on_death_restore_failed(reason):
        line = log_death_restore_failed(reason)
        with pending_lock:
            pending.append(line)

    def on_prune(name):
        line = log_prune(name)
        with pending_lock:
            pending.append(line)

    def on_prune_failed(reason):
        line = log_prune_failed(reason)
        with pending_lock:
            pending.append(line)

    def on_active_slot(slot):
        line = log_active_slot(slot)
        with pending_lock:
            pending.append(line)

    watcher = SaveWatcher(
        save_file, save_stem, read_vitals_fn, on_change, on_snapshot, on_snapshot_failed,
        on_death_restore, on_death_restore_failed, on_prune, on_prune_failed, on_active_slot,
        use_adaptive_rewind=False, slot_override=args.slot,
    )
    thread = threading.Thread(target=watcher.run, daemon=True)
    thread.start()

    def drain_pending():
        with pending_lock:
            while pending:
                overlay.push_event(pending.popleft())

    overlay.schedule(drain_pending)
    try:
        overlay.mainloop()
    finally:
        watcher.stop()


if __name__ == "__main__":
    main()
