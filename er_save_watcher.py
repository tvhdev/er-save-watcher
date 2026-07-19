#!/usr/bin/env python3
r"""
Multi-Game .sl2 / .sav Save File Watcher + Auto-Restore + On-Screen Overlay.

Watches a game save, snapshots it into numbered "<stem> - Kopie (N)<ext>"
files while the state is "clean" (health > 0 and the currency > 0), and
restores the latest clean snapshot after a death. Existing Kopie files are
never modified or deleted -- only new ones are added, and only watcher-
created snapshots beyond KOPIE_RETENTION_COUNT are pruned.

Games (-g/--game): er (Elden Ring, ER0000.sl2), dsr (Dark Souls Remastered,
DRAKS0005.sl2), ds3 (Dark Souls III, DS30000.sl2), ds2 (Dark Souls II:
Scholar of the First Sin, DS2SOFS0000.sl2), lop (Lies of P, .sav). The four
FromSoftware games are BND4 .sl2 containers read by read_vitals_er/dsr/ds3/
ds2; dsr/ds3/ds2 are AES-encrypted and need the 'cryptography' package (pip
install cryptography), er is unencrypted. lop is an Unreal Engine GVAS .sav
(read by read_vitals_lop, no extra deps) -- its currency is "Ergo"; see the
LOP_* constants block. See each read_vitals_* and the constants above it for
format details and credits to the reverse-engineering projects relied on.

Active-character detection (BND4 games only): a .sl2 holds up to 10
characters but only the one being played is rewritten on save, so the active
slot is the BND4 entry whose checksum changes between saves (entry_
fingerprints/changed_slot). The character entries are 0-9 for er/dsr/ds3 but
1-10 for ds2 (its entry 0 is a header rewritten every save); see
GAME_PROFILES' char_entries. Falls back to the first occupied slot until a
save is observed; override with -s/--slot. lop isn't a BND4 container, so
this no-ops for it; instead it auto-picks the most-recently-written
SaveData-*_Character_*.sav in the folder at startup (resolve_save_filename).

Death-restore gate: a restore fires only once the save has been unclean
AND quiet (not written) for DEATH_RESTORE_DELAY_SECONDS -- i.e. you've quit
to the main menu. A restore made while the game is still running just gets
overwritten by its next autosave; one made at the menu sticks (the game
reloads from disk on Continue). Restores are verified with copy_and_verify
(MD5 compare + retry).

Usage (Windows, Python 3 installed):
    python er_save_watcher.py -g {er,dsr,ds3,ds2,lop} [-s SLOT] [save_dir]

    Launched with no arguments (e.g. the exe double-clicked), it prints/shows
    a short help and exits. save_dir holds the save file; for the BND4 games
    it's auto-detected (see GAME_PROFILES) when there's exactly one candidate
    subfolder, but for lop you pass the SaveGames\<id> folder explicitly.

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

# ---- Dark Souls II: Scholar of the First Sin specifics -------------------

# Also a BND4 container with AES-128-CBC entries (key from jtesta/souls_givifier), but laid out differently
# from DSR/DS3 in two ways the rest of the code has to account for:
#   1. The occupancy table AND per-slot names live in entry #0 (not entry #10), one record per slot
#      DS2_SLOT_STRIDE bytes apart: an occupancy byte at DS2_OCCUPANCY_BYTE_BASE + DS2_SLOT_STRIDE*i and a
#      UTF-16 name at DS2_NAME_BASE + DS2_SLOT_STRIDE*i (up to DS2_NAME_MAX_LEN chars).
#   2. A character's actual save data is NOT in the same entry as its occupancy/name -- occupancy index i
#      (0-9) maps to BND4 entry i+1, so the 10 characters occupy entries 1-10 and entry #0 is a header/menu
#      entry. This is why DS2's active-character detection scans entries 1-10 (char_entries below) while the
#      other games scan 0-9: DS2's entry #0 is rewritten on every save and would be a constant false positive.
# Within a character entry, current souls is at DS2_SOULS_REL_OFFSET (fluctuates up/down as you earn/spend;
# the +4/+8 ints next to it are SOUL MEMORY -- monotonic, never resets, same trap as DSR/DS3, avoid) and the
# character's BASE max HP at DS2_HEALTH_REL_OFFSET. Souls was verified empirically against a live in-game
# value. The HP field is the STORED BASE value and does NOT include the bonuses HP-boosting rings apply at
# runtime, so it reads lower than the on-screen number when such a ring is worn (verified: stored 1054 shows
# in-game as 1404 with a +HP ring, 1244 with another) -- it's logged for information only, not used for
# anything. Crucially, DS2 death detection runs entirely on SOULS, not HP: DS2 only writes the save once you
# respawn at a bonfire (always at full HP), so the saved HP never reads 0 the way it does in the other three
# games -- it's souls dropping to 0 on death (until you recover your bloodstain) that signals it, exactly the
# clean-state rule is_clean_state already enforces. Names/occupancy mirror ds2_get_slot_occupancy() in souls_givifier.
DS2_AES_KEY = bytes.fromhex("599f9b699640a55236ee2d70835ec744")
DS2_OCCUPANCY_BYTE_BASE = 892  # occupancy byte for slot 0 within decrypted entry #0
DS2_NAME_BASE = 1286  # UTF-16 name for slot 0 within decrypted entry #0
DS2_SLOT_STRIDE = 496  # bytes between consecutive slots' occupancy/name records
DS2_NAME_MAX_LEN = 14  # characters (UTF-16)
DS2_SOULS_REL_OFFSET = 60  # current (spendable) souls within a character entry; +4/+8 are SOUL MEMORY (avoid)
DS2_HEALTH_REL_OFFSET = 72  # base max HP within a character entry (excludes runtime ring bonuses; informational only -- death detection uses souls)

# ---- Lies of P specifics -------------------------------------------------

# Lies of P is an Unreal Engine game, so its saves are a completely different animal from the four
# FromSoftware games above: not a BND4 .sl2 container but an uncompressed GVAS blob (magic 'GVAS') with
# human-readable UE property names. There are no per-character BND4 slots, so the active-slot detection
# simply doesn't apply -- entry_fingerprints() returns None on a non-BND4 file and detection no-ops.
# DEATH SIGNAL (verified empirically with a controlled death + recovery on a real save): Lies of P DOES drop
# your carried ergo on death and make you run back to a marker to reclaim it, exactly like souls/runes -- I
# was just reading the wrong fields at first. In the save it shows up as two MUTUALLY EXCLUSIVE properties:
#   - AcquisitionSoul -- your carried wallet ergo. Present while you hold ergo; ABSENT once you've died and
#                        dropped it.
#   - RemainErgo      -- the pending death-drop waiting at the marker (BaseErgo is its original amount).
#                        ABSENT when you have no outstanding drop; PRESENT (== the dropped amount) from the
#                        moment you die until you reclaim it.
# So a pending death-drop (RemainErgo present) is the unambiguous "you died and haven't recovered" signal --
# and unlike the wallet it can't be confused with a broke-but-alive character (0 wallet, no drop). We key the
# clean-state on it: clean == no pending drop. read_vitals_lop returns (wallet, drop) and is_clean_state_lop
# treats drop == 0 as clean. (An earlier attempt keyed on the HP property SecondStat_HeadthPoint vanishing on
# death; that happens too, but it's a transient save-write artifact that also fires on non-deaths, so it's out.)
#
# Files are named SaveData-<slot>_Character_<n>.sav; the game writes a primary+backup pair per save (written a
# few seconds apart, and they can briefly hold different states), and there can be several slots (different
# characters). We watch/restore ONE file -- the most-recently-written SaveData-*_Character_*.sav, resolved at
# startup (_resolve_lop_save_filename) -- the active character. NOTE (still to confirm live): we restore only
# the watched file; if a death-restore ever fails to "stick" it's most likely the game reloaded the sibling
# backup instead, in which case we'd restore over the whole pair.
LOP_GVAS_MAGIC = b"GVAS"
LOP_WALLET_PROPERTY = "AcquisitionSoul"  # carried ergo; present while held, ABSENT once dropped on death (display only)
LOP_DROP_PROPERTY = "RemainErgo"  # pending death-drop at the marker; PRESENT(>0) == you died & haven't recovered == the death signal
LOP_SAVE_GLOB = "SaveData-*_Character_*.sav"  # per-character save files; excludes Account_*/OptionSlot

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


def _find_default_save_dir_ds2():
    """Dark Souls II: Scholar of the First Sin saves live at %APPDATA%\\DarkSoulsII\\<id>\\."""
    appdata = os.environ.get("APPDATA")
    return _find_default_subfolder_dir(os.path.join(appdata, "DarkSoulsII")) if appdata else None


def _find_default_save_dir_lop():
    """Lies of P saves live under its Steam install (e.g. <SteamLibrary>\\steamapps\\common\\Lies of P\\
    LiesofP\\Saved\\SaveGames\\<id>\\). That library can be on any drive, so there's nothing reliable to
    auto-detect -- the user passes the SaveGames\\<id> folder explicitly."""
    return None


def _resolve_lop_save_filename(save_dir):
    """Lies of P has no single fixed save filename: a folder holds SaveData-<slot>_Character_<n>.sav files
    for possibly several characters, each written as a primary+backup pair. Return the basename of the most-
    recently-modified one (the character currently being played), or None if the folder has no such file."""
    import glob
    files = [f for f in glob.glob(os.path.join(save_dir, LOP_SAVE_GLOB)) if " - Kopie" not in os.path.basename(f)]
    if not files:
        return None
    return os.path.basename(max(files, key=os.path.getmtime))


# SaveData-<slot>_Character_<n>.sav: <slot> identifies the character; _Character_1 / _Character_2 are the
# A/B double-buffer that the game writes alternately for that one character (they leapfrog -- either can hold
# the freshest state at a given instant). "The pair" below = both buffers of a single character.
_LOP_PAIR_RE = re.compile(r"(SaveData-\d+)_Character_\d+\.sav$", re.IGNORECASE)


def _lop_pair_paths(path):
    """Return every file that is part of the same LoP save as `path` (the A/B double-buffer pair for one
    character), newest last. Falls back to just [path] if the name doesn't match the expected pattern."""
    import glob
    base = os.path.basename(path)
    m = _LOP_PAIR_RE.match(base)
    if not m:
        return [path]
    siblings = [f for f in glob.glob(os.path.join(os.path.dirname(path), m.group(1) + "_Character_*.sav"))
                if " - Kopie" not in os.path.basename(f)]
    return sorted(siblings or [path], key=os.path.getmtime)


# char_entries = the BND4 entry indices that hold character data, used for active-character detection
# (entry_fingerprints/changed_slot). er/dsr/ds3 store characters directly in entries 0-9; DS2 stores them in
# entries 1-10 (its entry 0 is a header that's rewritten every save -- see the DS2 comment block above).
# Lies of P isn't a BND4 container at all, so char_entries is unused for it (detection no-ops) and instead
# it supplies resolve_save_filename to pick the active .sav out of the folder at startup.
GAME_PROFILES = {
    "er": {
        "save_filename": "ER0000.sl2",
        "find_default_save_dir": _find_default_save_dir_er,
        "char_entries": range(10),
    },
    "dsr": {
        "save_filename": "DRAKS0005.sl2",
        "find_default_save_dir": _find_default_save_dir_dsr,
        "char_entries": range(10),
    },
    "ds3": {
        "save_filename": "DS30000.sl2",
        "find_default_save_dir": _find_default_save_dir_ds3,
        "char_entries": range(10),
    },
    "ds2": {
        "save_filename": "DS2SOFS0000.sl2",
        "find_default_save_dir": _find_default_save_dir_ds2,
        "char_entries": range(1, 11),
    },
    "lop": {
        "save_filename": None,  # resolved from the folder at startup (see resolve_save_filename)
        "resolve_save_filename": _resolve_lop_save_filename,
        "find_default_save_dir": _find_default_save_dir_lop,
        "char_entries": range(10),  # unused for lop (not a BND4 container); detection no-ops
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


def entry_fingerprints(path, char_entries=range(10)):
    """
    For active-character detection across all four games (all BND4): returns
    {entry_index: checksum_bytes} for the BND4 entries in char_entries, reading
    just the 16-byte per-slot checksum at each entry's data_offset (cheap -- no
    full read or decryption). FromSoftware rewrites only the active character's
    entry on save, so the entry whose checksum changes between two saves is the
    one being played. char_entries scopes which entries hold character data:
    0-9 for er/dsr/ds3, but 1-10 for DS2 (whose entry 0 is a header rewritten
    every save -- including it would be a constant false positive). Returns
    None on any failure.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(DSR_BND4_HEADER_LEN + DSR_BND4_ENTRY_HEADER_LEN * 16)
            if header[0:4] != b"BND4":
                return None
            num_entries = struct.unpack_from("<i", header, 12)[0]
            out = {}
            for i in char_entries:
                if i >= num_entries:
                    continue
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


def _ds2_occupied_entries(raw, entries):
    """
    DS2's entry #0 holds the occupancy table for the 10 character slots, one
    byte per slot at DS2_OCCUPANCY_BYTE_BASE + DS2_SLOT_STRIDE*i. A non-zero
    byte means slot i is occupied, and that character's save data lives in
    BND4 entry i+1. Returns the list of occupied character ENTRY indices
    (1-10), lowest first. Mirrors ds2_get_slot_occupancy() in souls_givifier.
    """
    size, data_offset = entries[0]
    decrypted = _bnd4_decrypt_entry(raw, size, data_offset, DS2_AES_KEY)
    occupied = []
    for i in range(10):
        if decrypted[DS2_OCCUPANCY_BYTE_BASE + DS2_SLOT_STRIDE * i] != 0:
            occupied.append(i + 1)
    return occupied


def read_vitals_ds2(path, slot=None):
    """Returns (health, souls) for the DS2 SOTFS save at path, or (None, None) on any read failure.
    slot is the BND4 entry index of the active character (1-10); None (or one that isn't occupied)
    falls back to the first occupied character entry. Unlike DSR/DS3, occupancy lives in entry #0 while
    the character's data is in entry i+1 (see the DS2 comment block)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, None
    try:
        entries = _bnd4_parse_entries(raw)
        occupied = _ds2_occupied_entries(raw, entries)
        if not occupied:
            return None, None
        if slot is None or slot not in occupied:
            slot = occupied[0]
        size, data_offset = entries[slot]
        decrypted = _bnd4_decrypt_entry(raw, size, data_offset, DS2_AES_KEY)
        health = struct.unpack_from("<I", decrypted, DS2_HEALTH_REL_OFFSET)[0]
        souls = struct.unpack_from("<I", decrypted, DS2_SOULS_REL_OFFSET)[0]
        return health, souls
    except (ValueError, struct.error, IndexError):
        return None, None
    except ImportError:
        return None, None


def _gvas_read_fstring(raw, pos):
    """
    Reads a UE FString at pos: int32 length (which INCLUDES the trailing null),
    then that many bytes. Returns (text, next_pos), or (None, pos) if the length
    looks implausible (so a coincidental byte match doesn't run us off the rails).
    """
    n = struct.unpack_from("<i", raw, pos)[0]
    if n <= 0 or n > 512:
        return None, pos
    return raw[pos + 4:pos + 4 + n - 1].decode("ascii", "replace"), pos + 4 + n


def _gvas_read_int_property(raw, name):
    """
    Locate a UE IntProperty by name in an uncompressed GVAS blob and return its
    int32 value, or None if not found. UE serializes a property as: FString name,
    FString type, int64 payload-size, 1 byte (optional-GUID present flag), then
    the value -- for IntProperty a 4-byte int. We match the name as a proper
    FString (verifying the int32 length-with-null prefix right before it) so we
    don't latch onto the same bytes appearing elsewhere, then read the int after
    the 'IntProperty' type tag. This is the same "find a known field by name and
    read a fixed distance from it" tactic used for DS3, just for UE's format.
    """
    nb = name.encode("ascii")
    i = 0
    while True:
        j = raw.find(nb, i)
        if j < 0:
            return None
        i = j + 1
        if j < 4 or struct.unpack_from("<i", raw, j - 4)[0] != len(nb) + 1:
            continue  # not an FString-prefixed name here -- a coincidental byte match
        typ, pos = _gvas_read_fstring(raw, j + len(nb) + 1)  # skip name bytes + null terminator
        if typ != "IntProperty":
            continue
        # int64 payload size (8 bytes) + 1 byte has-guid flag, then the int32 value
        return struct.unpack_from("<i", raw, pos + 8 + 1)[0]


def read_vitals_lop(path, slot=None):
    """Returns (wallet, drop) for the Lies of P save at path, or (None, None) if the file can't be read.
    slot is ignored (Lies of P isn't a BND4 multi-slot container). wallet = carried ergo (AcquisitionSoul;
    0 when the property is absent == dropped on death); drop = the pending death-drop amount (RemainErgo;
    0 when absent == nothing outstanding). is_clean_state_lop keys on drop == 0. These map onto the generic
    (health, souls) slots so the rest of the pipeline -- logging, snapshotting, restore -- works unchanged."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, None
    if raw[:4] != LOP_GVAS_MAGIC:
        return None, None
    try:
        wallet = _gvas_read_int_property(raw, LOP_WALLET_PROPERTY)
        drop = _gvas_read_int_property(raw, LOP_DROP_PROPERTY)
    except struct.error:
        return None, None
    # Absent property -> 0. Wallet absent means you dropped it on death; drop absent means nothing outstanding.
    return (wallet or 0), (drop or 0)


def _lop_debug_fields(path):
    """Diagnostic dump for Lies of P: for the watched file AND its Character-pair sibling(s), report the raw
    AcquisitionSoul (wallet) and RemainErgo (drop) properties and mtime -- so the log shows exactly which file
    holds the pending-drop death state and when the pair fall out of sync."""
    import glob
    directory = os.path.dirname(path)
    match = re.match(r"(SaveData-\d+)_Character_\d+\.sav$", os.path.basename(path))
    group = match.group(1) if match else None
    files = sorted(glob.glob(os.path.join(directory, f"{group}_Character_*.sav"))) if group else [path]
    parts = []
    for f in files:
        try:
            raw = open(f, "rb").read()
            wallet = _gvas_read_int_property(raw, LOP_WALLET_PROPERTY)   # AcquisitionSoul (carried ergo)
            drop = _gvas_read_int_property(raw, LOP_DROP_PROPERTY)       # RemainErgo (ergo left in the drop)
            base = _gvas_read_int_property(raw, "BaseErgo")              # original amount of the drop
            stolen = _gvas_read_int_property(raw, "StolenDropErgo")      # ergo an enemy stole from the drop
            parts.append(f"{os.path.basename(f)}[wallet={wallet} drop={drop} base={base} stolen={stolen} mtime={os.path.getmtime(f):.0f}]")
        except OSError:
            pass
    return " | ".join(parts)


def is_clean_state(health, souls):
    """
    Default (FromSoftware) clean check: a snapshot/state is only trustworthy as
    a "good" checkpoint if the player is both alive (health > 0) AND has already
    reclaimed their runes (souls > 0). The gap right after respawning -- alive
    again, but runes still sitting unclaimed at the death location -- is excluded
    too, not just the moment of death itself. None readings are treated as
    "allow" since we can't determine the state and don't want to block on that.

    Lies of P uses a DIFFERENT check (is_clean_state_lop) -- see there for why.
    """
    if health is None or souls is None:
        return True
    return health != 0 and souls != 0


def is_clean_state_lop(wallet, drop):
    """
    Lies of P clean check: clean iff there is NO pending death-drop (drop == 0).
    See the LOP_* constants block for the full reasoning. Keying on the drop
    (rather than the wallet) is what makes this robust:
      - died, not recovered -> drop > 0  -> UNCLEAN (restore-worthy),
      - alive holding ergo   -> drop == 0 -> clean,
      - alive but broke (spent all ergo, no drop) -> drop == 0 -> clean, NOT a
        false death.
    (Parameter names wallet/drop mirror read_vitals_lop's return; they occupy
    the generic health/souls slots the watcher passes in.)
    """
    return drop == 0


def is_restore_point_lop(wallet, drop):
    """
    Lies of P restore-point check: a state is worth snapshotting / restoring to
    ONLY if you're holding ergo (wallet > 0) AND have no pending death-drop
    (drop == 0).

    This is STRICTER than is_clean_state_lop (the death trigger). The trigger
    keys on drop alone so a broke-but-alive state (wallet == 0, drop == 0) is not
    mistaken for a death. But we never want to *restore you into* a 0-ergo state
    -- that would throw away ergo for no benefit -- so the restore-point predicate
    additionally requires wallet > 0. Net effect: die -> restored to the last
    Stargazer where you actually had ergo in your wallet and nothing dropped.
    """
    return wallet > 0 and drop == 0


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


def _kopie_name_re(save_stem, save_ext=".sl2"):
    return re.compile(rf"^{re.escape(save_stem)} - Kopie(?: \((\d+)\))?{re.escape(save_ext)}$")


def list_numbered_kopies(save_dir, save_stem, save_ext=".sl2"):
    """Return [(number, path), ...] for files matching the Kopie naming scheme, newest (highest number) first."""
    name_re = _kopie_name_re(save_stem, save_ext)
    found = []
    for name in os.listdir(save_dir):
        match = name_re.match(name)
        if match:
            number = int(match.group(1)) if match.group(1) else 1
            found.append((number, os.path.join(save_dir, name)))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return found


def next_kopie_path(save_dir, save_stem, save_ext=".sl2"):
    existing = list_numbered_kopies(save_dir, save_stem, save_ext)
    next_number = existing[0][0] + 1 if existing else 1
    name = f"{save_stem} - Kopie{save_ext}" if next_number == 1 else f"{save_stem} - Kopie ({next_number}){save_ext}"
    return os.path.join(save_dir, name)


def _watcher_state_file(save_stem):
    # Per-save-stem, so tracking two different games' baselines from the same _APP_DIR never collides.
    return os.path.join(_APP_DIR, f"watcher_state_{save_stem}.txt")


def load_or_init_kopie_baseline(save_dir, save_stem, save_ext=".sl2"):
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
    existing = list_numbered_kopies(save_dir, save_stem, save_ext)
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
                 use_adaptive_rewind=False, slot_override=None, char_entries=range(10), save_ext=".sl2",
                 on_debug=None, debug_fields_fn=None, clean_state_fn=is_clean_state,
                 restore_point_fn=None, follow_pair=False, vital_labels=("health", "souls")):
        self.path = path
        self.save_dir = os.path.dirname(path)
        self.save_stem = save_stem
        self.save_ext = save_ext
        # Lies of P writes each save as an A/B double-buffer pair (_Character_1 / _Character_2) that leapfrog,
        # so the freshest state can be in either file and a restore must overwrite BOTH. When follow_pair is on
        # we track the pair and re-point self.path at whichever member is newest before each read. save_stem
        # stays FIXED (from startup) so our Kopie names don't flip between the two buffers. Off (FromSoft
        # games) -> a single fixed file, pair == [path].
        self.follow_pair = follow_pair
        self.pair_paths = _lop_pair_paths(path) if follow_pair else [path]
        self.read_vitals = read_vitals_fn
        self.char_entries = char_entries
        self.is_clean = clean_state_fn  # per-game clean-state predicate (FromSoft default; lop overrides)
        # Predicate for states worth SNAPSHOTTING / RESTORING TO. Stricter than
        # is_clean (the death trigger): defaults to is_clean, but lop passes
        # is_restore_point_lop (wallet > 0 AND drop == 0) so we never snapshot or
        # restore into a 0-ergo state. See is_restore_point_lop for the rationale.
        self.is_restore_point = restore_point_fn or clean_state_fn
        self.vital_labels = vital_labels  # (name0, name1) for the two vitals in logs; lop = ("wallet", "drop")
        self.on_debug = on_debug  # log-only diagnostic sink (or None to disable)
        self.debug_fields_fn = debug_fields_fn  # optional path -> extra-fields string, logged on each change
        self._prev_clean = None  # for logging clean<->unclean transitions in _check_death
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
        self._observed_clean = False  # have we seen a clean state since startup? gates restore (see _check_death)
        self._death_restore_skip = 0  # adaptive rewind only: how many extra clean snapshots back to skip
        self._last_death_restore_monotonic = None  # adaptive rewind only: when the last death-restore happened
        self._kopie_baseline = load_or_init_kopie_baseline(self.save_dir, self.save_stem, self.save_ext)
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
        kopies = list_numbered_kopies(self.save_dir, self.save_stem, self.save_ext)
        if len(kopies) >= 2:
            cand = changed_slot(
                entry_fingerprints(kopies[1][1], self.char_entries),
                entry_fingerprints(kopies[0][1], self.char_entries),
            )
            if cand is not None:
                self._active_slot = cand

    def _follow_active_file(self):
        """LoP double-buffers each save across the _Character_1/_Character_2 pair, and the two leapfrog, so the
        freshest state may be in either file. Point self.path at whichever pair member is newest before we read,
        so we never watch a stale buffer. No-op for the single-file (FromSoft) games."""
        if not self.follow_pair:
            return
        try:
            newest = max(self.pair_paths, key=os.path.getmtime)
        except (OSError, ValueError):
            return  # a member briefly missing/locked mid-write; keep the current path and retry next tick
        if newest != self.path:
            self.path = newest

    def _refresh_active_slot(self, path):
        if self.slot_override is not None:
            return
        fingerprints = entry_fingerprints(path, self.char_entries)
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
        self._follow_active_file()  # LoP: track whichever buffer of the pair is newest before reading
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
        if self.on_debug is not None and self.debug_fields_fn is not None:
            try:
                self.on_debug(f"change: delta={delta} clean={self.is_clean(health, souls)} :: {self.debug_fields_fn(self.path)}")
            except Exception as exc:  # diagnostics must never break the watcher
                self.on_debug(f"change: debug_fields_fn error: {exc}")
        # Snapshot any restore-worthy state, INCLUDING the very first (baseline) read where delta is None. A
        # watcher started with no prior Kopie files must capture a restore point immediately -- otherwise a death
        # before the game's next on-disk save (Lies of P only saves at checkpoints, so that can be minutes)
        # has nothing to roll back to. This is exactly what bit the first Lies of P death test.
        # is_restore_point is stricter than is_clean for lop (requires wallet > 0), so we don't snapshot a
        # broke-but-alive state and later restore you into having no ergo.
        if self.is_restore_point(health, souls):
            self._snapshot_change()

    def _check_death(self):
        # Only restore once the save file has been UNCLEAN *and* quiet -- not written for
        # DEATH_RESTORE_DELAY_SECONDS. The quiet part is key: while you're still playing after a death the game
        # keeps rewriting the save with the (still-unclean) live state every few seconds, and any restore we
        # made would be instantly overwritten. The file only goes quiet once you've quit to the main menu, and
        # a restore done then sticks (the game reloads from disk on Continue). We read the file's actual mtime
        # each check (rather than a timer maintained elsewhere) so this is immune to poll-ordering races -- and
        # since our own restore write bumps the mtime, it also can't immediately re-fire on its own restore.
        self._follow_active_file()  # LoP: judge quiet-time / vitals from whichever buffer is newest
        try:
            seconds_since_write = time.time() - os.path.getmtime(self.path)
        except OSError:
            return
        health, souls = self._read_vitals(self.path)
        if health is None or souls is None:
            return  # couldn't read a vital (file locked mid-write) -- try again next tick
        clean = self.is_clean(health, souls)
        if self.on_debug is not None:
            # Log the death-check verdict on every clean<->unclean transition, and every tick while unclean
            # (so a death episode shows the quiet-gate countdown). Silent during normal clean play.
            vitals = f"{self.vital_labels[0]}={health} {self.vital_labels[1]}={souls}"
            if clean != self._prev_clean:
                self.on_debug(f"death-check: -> {'CLEAN' if clean else 'UNCLEAN'} ({vitals} quiet={seconds_since_write:.1f}s)")
                if not clean and self.debug_fields_fn is not None:
                    # A death was just detected -- log exactly what the game wrote to the save on death.
                    try:
                        self.on_debug(f"death-check: SAVED-ON-DEATH :: {self.debug_fields_fn(self.path)}")
                    except Exception as exc:
                        self.on_debug(f"death-check: death-dump error: {exc}")
            elif not clean and not self._death_restore_pending:
                # Log the quiet-gate countdown only until the restore is attempted; once pending, stop
                # logging every tick so a lingering unclean state (e.g. restore failed) can't flood the log.
                self.on_debug(f"death-check: still UNCLEAN ({vitals} quiet={seconds_since_write:.1f}s/{DEATH_RESTORE_DELAY_SECONDS}s)")
            self._prev_clean = clean
        if clean:
            self._observed_clean = True  # from here on, an unclean read is a death we actually witnessed
            self._death_restore_pending = False
            return
        if not self._observed_clean:
            # The save was ALREADY unclean when we started watching -- a death (or, for Lies of P, a still-
            # pending ergo drop) that happened before us. Don't restore it: acting on a pre-existing unclean
            # state means clobbering whatever the player has done since (e.g. an in-game recovery the game
            # hasn't written to disk yet). Only ever restore a clean->unclean transition we saw happen live.
            return
        if self._death_restore_pending:
            return  # already attempted a restore for this unclean episode; wait for it to clear
        if seconds_since_write < DEATH_RESTORE_DELAY_SECONDS:
            return  # file written recently (you're still playing) -- wait for it to go quiet (main menu)
        self._death_restore_pending = True
        self._restore_after_death(health, souls)

    def _snapshot_change(self):
        try:
            dest = next_kopie_path(self.save_dir, self.save_stem, self.save_ext)
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
            (number, p) for number, p in list_numbered_kopies(self.save_dir, self.save_stem, self.save_ext)
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
            kopies = list_numbered_kopies(self.save_dir, self.save_stem, self.save_ext)
        except OSError as exc:
            self.on_death_restore_failed(str(exc))
            return
        clean_candidates = []  # newest first; only ones that are a valid restore point per this game's rule
        for _, candidate in kopies:
            health, souls = self._read_vitals(candidate)
            if health is not None and souls is not None and self.is_restore_point(health, souls):
                clean_candidates.append(candidate)
        if not clean_candidates:
            self.on_death_restore_failed("no clean snapshot found to restore from")
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
        # Overwrite EVERY file of the save. FromSoft games -> the single watched file. Lies of P -> both
        # buffers of the A/B pair, so the restored state loads no matter which buffer the game reads on
        # Continue (writing only one could leave the game loading the stale other buffer). One-way copies; the
        # source Kopie is never renamed/moved/deleted.
        targets = self.pair_paths if self.follow_pair else [self.path]
        for target in targets:
            ok, exc = copy_and_verify(source, target)
            if not ok:
                reason = str(exc) if exc else f"MD5 of restored file never matched source after {RESTORE_VERIFY_MAX_ATTEMPTS} attempts"
                self.on_death_restore_failed(reason)
                return
        self._follow_active_file()  # re-point self.path at the newest just-written buffer before re-baselining
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


def log_change(size, delta, short_hash, health, souls, labels=("health", "souls")):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    delta_str = "first snapshot" if delta is None else f"{delta:+d} bytes"
    vitals = f"{labels[0]}={health} {labels[1]}={souls}"
    line = f"[{timestamp}] size={size} ({delta_str}) hash={short_hash} {vitals}"
    _write_log(line)
    return f"{timestamp}  {delta_str}  #{short_hash}  {vitals}"


def log_debug(msg):
    # Diagnostic-only: written to the log file, NOT pushed to the overlay (so it can't spam the on-screen box).
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write_log(f"[{timestamp}] DEBUG: {msg}")


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


def log_death_restore(source_name, health, souls, skip, labels=("health", "souls")):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    skip_str = f" (skipped {skip} more recent clean snapshot{'s' if skip != 1 else ''})" if skip else ""
    vitals = f"{labels[0]}={health}, {labels[1]}={souls}"
    line = f"[{timestamp}] UNCLEAN STATE ({vitals}) -- RESTORED '{source_name}' -> live save{skip_str}"
    _write_log(line)
    return f"{timestamp}  RESTORED '{source_name}' (was {vitals}){skip_str}"


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


def _short_help():
    prog = os.path.basename(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]) or "er_save_watcher.py"
    return (
        ".sl2 / .sav Save Watcher -- snapshots your save while you play and auto-restores\n"
        "your last good checkpoint after you die.\n"
        "\n"
        f"Usage:  {prog} -g GAME [SAVE_FOLDER]\n"
        "\n"
        "Games (-g):\n"
        "  er   Elden Ring             (save folder auto-detected)\n"
        "  dsr  Dark Souls Remastered  (save folder auto-detected)\n"
        "  ds3  Dark Souls III         (save folder auto-detected)\n"
        "  ds2  Dark Souls II: SOTFS   (save folder auto-detected)\n"
        "  lop  Lies of P              (pass the SaveGames\\<id> folder)\n"
        "\n"
        "Examples:\n"
        f"  {prog} -g er\n"
        f"  {prog} -g lop \"G:\\SteamLibrary\\steamapps\\common\\Lies of P\\LiesofP\\Saved\\SaveGames\\7062576\"\n"
        "\n"
        f"Full options:  {prog} -h"
    )


def _show_help_dialog(text):
    # The exe is built --windowed (no console), so anything printed to stderr is invisible when it's double-
    # clicked. Pop the same help text in a small GUI box too. Best-effort: silently skip if there's no display.
    try:
        import tkinter.messagebox as messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo("Save Watcher", text)
        root.destroy()
    except Exception:
        pass


class _HelpfulParser(argparse.ArgumentParser):
    # Route argparse errors (bad/missing/unknown arguments) through the GUI box as well as stderr -- same
    # windowed-exe reason as above -- so a mistyped option isn't a silent no-op, then exit.
    def error(self, message):
        text = f"{message}\n\n{_short_help()}"
        print(text, file=sys.stderr)
        _show_help_dialog(text)
        self.exit(2)


def parse_args():
    if len(sys.argv) == 1:  # no arguments at all (e.g. the exe was double-clicked) -> show help and stop
        print(_short_help(), file=sys.stderr)
        _show_help_dialog(_short_help())
        sys.exit(0)

    pre_parser = _HelpfulParser(add_help=False)
    pre_parser.add_argument("-g", "--game", choices=sorted(GAME_PROFILES), default="er")
    pre_args, _ = pre_parser.parse_known_args()
    profile = GAME_PROFILES[pre_args.game]
    auto_detected = profile["find_default_save_dir"]()
    save_target = profile["save_filename"] or "the active SaveData-*.sav (auto-picked from the folder)"

    parser = _HelpfulParser(description="FromSoftware .sl2 / Lies of P .sav save watcher with auto-restore and on-screen overlay.")
    parser.add_argument(
        "-g", "--game", choices=sorted(GAME_PROFILES), default="er",
        help="Which game's save format to use (default: er). Determines the save filename and Kopie naming.",
    )
    parser.add_argument(
        "save_dir", nargs="?", default=auto_detected,
        help=f"Path to the save folder containing {save_target}"
             + (f" (default, auto-detected: {auto_detected})" if auto_detected else
                " (required: couldn't auto-detect a unique save folder for this game)"),
    )
    parser.add_argument(
        "-s", "--slot", type=int, default=None,
        help="Force a specific character slot (0-9) instead of auto-detecting the active one. "
             "Normally unnecessary -- the watcher detects which character is being played by which "
             "save slot changes -- but useful to override if detection picks wrong. (Ignored for lop.)",
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
    resolver = profile.get("resolve_save_filename")
    if resolver is not None:  # lop: no fixed filename -- pick the active save out of the folder
        save_filename = resolver(args.save_dir)
        if save_filename is None:
            print(
                f"ERROR: couldn't find a {args.game} save file (matching '{LOP_SAVE_GLOB}') in:\n"
                f"  {args.save_dir}\n"
                "Point the tool at the game's SaveGames\\<id> folder.",
                file=sys.stderr,
            )
            sys.exit(1)
    save_stem, save_ext = os.path.splitext(save_filename)
    save_file = os.path.join(args.save_dir, save_filename)
    if args.game == "lop":
        # Name Kopie files after the character's stable SaveData-<slot> prefix, NOT the raw filename: the raw
        # name carries the A/B buffer number (_Character_1 / _Character_2) that flips depending on which buffer
        # was newest at startup, so a buffer-derived stem would make each run create/look-for a different Kopie
        # series. The slot prefix is stable across runs and, lacking "_Character_", keeps our Kopie files from
        # matching the save globs. (save_file still points at the actual buffer we watch.)
        m = _LOP_PAIR_RE.match(save_filename)
        if m:
            save_stem = m.group(1)

    if args.game in ("dsr", "ds3", "ds2"):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            print(
                f"ERROR: {args.game} support requires the 'cryptography' package, which isn't installed.\n"
                "Install it with:  pip install cryptography",
                file=sys.stderr,
            )
            sys.exit(1)
        read_vitals_fn = {"dsr": read_vitals_dsr, "ds3": read_vitals_ds3, "ds2": read_vitals_ds2}[args.game]
    elif args.game == "lop":
        read_vitals_fn = read_vitals_lop
    else:
        read_vitals_fn = read_vitals_er

    overlay = Overlay(save_filename)
    pending = deque()
    pending_lock = threading.Lock()

    # For Lies of P the two vitals are the ergo wallet and the pending death-drop, not health/souls.
    vital_labels = ("wallet", "drop") if args.game == "lop" else ("health", "souls")

    def on_change(size, delta, short_hash, health, souls):
        line = log_change(size, delta, short_hash, health, souls, vital_labels)
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
        line = log_death_restore(source_name, health, souls, skip, vital_labels)
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

    def on_debug(msg):  # diagnostic, log-file only (never touches the overlay)
        log_debug(msg)

    debug_fields_fn = _lop_debug_fields if args.game == "lop" else None
    clean_state_fn = is_clean_state_lop if args.game == "lop" else is_clean_state
    restore_point_fn = is_restore_point_lop if args.game == "lop" else None

    watcher = SaveWatcher(
        save_file, save_stem, read_vitals_fn, on_change, on_snapshot, on_snapshot_failed,
        on_death_restore, on_death_restore_failed, on_prune, on_prune_failed, on_active_slot,
        use_adaptive_rewind=False, slot_override=args.slot, char_entries=profile["char_entries"],
        save_ext=save_ext, on_debug=on_debug, debug_fields_fn=debug_fields_fn, clean_state_fn=clean_state_fn,
        restore_point_fn=restore_point_fn, follow_pair=(args.game == "lop"), vital_labels=vital_labels,
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
