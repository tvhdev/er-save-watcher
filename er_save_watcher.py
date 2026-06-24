#!/usr/bin/env python3
r"""
Multi-Game .sl2 Save File Watcher + Auto-Restore + On-Screen Overlay.

Watches a FromSoftware .sl2 save file, snapshots it into numbered
"<stem> - Kopie (N).sl2" files whenever the state is "clean" (health > 0
and souls > 0 -- see is_clean_state()), and can restore an earlier
snapshot back over the live save. Pre-existing Kopie files and
<stem>-backup.sl2 are never renamed, overwritten, or deleted.

Two games are supported (-g/--game): "er" (Elden Ring, ER0000.sl2) and
"dsr" (Dark Souls Remastered, DRAKS0005.sl2). They use different save
formats entirely -- see GAME_PROFILES below and the per-game read_vitals
functions:
- Elden Ring slots are unencrypted; health/souls are located by parsing
  the save structurally (header, the variable-length ga_items array,
  then PlayerGameData) rather than via a fixed offset, since item changes
  shift everything after that array -- see _find_player_game_data_start()
  and read_vitals_er(). Credit: offset/struct layout from
  ClayAmore/ER-Save-Editor (PlayerGameData/GaItem definitions) and
  Ariescyn/EldenRing-Save-Manager (slot/checksum layout).
- Dark Souls Remastered slots are individually AES-128-CBC encrypted
  (key below) inside a generic BND4 container -- see
  _bnd4_parse_entries(), _dsr_decrypt_entry(), and read_vitals_dsr().
  health/souls sit at fixed offsets once decrypted (no variable-length
  array to walk, unlike Elden Ring), verified against two real characters
  in this save -- including, critically, distinguishing CURRENT
  (spendable) souls (offset 224) from lifetime SOULS MEMORY (offset 228,
  monotonically non-decreasing, never resets on death/spending). An
  earlier version of this code used 228 for death detection, which
  appeared to verify correctly at first only because souls_memory hadn't
  yet diverged from current souls in those checks -- it never actually
  read 0 after a real death, so the restore never fired. Only the first
  occupied character slot is tracked if more than one exists. Credit:
  BND4 parsing and the AES key from jtesta/souls_givifier; the fixed
  offsets were found empirically here (cross-checked against a documented
  field table for original, unencrypted Dark Souls in tarvitz/dsfp, which
  roughly but not exactly lines up -- DSR's internal layout has shifted
  slightly since). Requires the third-party 'cryptography' package (pip
  install cryptography) -- only imported when -g/--game dsr is actually
  used, so Elden Ring users need no extra install.

Restore path: if health/souls go unclean and stay that way for
DEATH_RESTORE_DELAY_SECONDS, restores the latest clean snapshot. The
delay matters because the game holds authoritative state in memory while
running and can overwrite our restore with its own next autosave -- see
_check_death()/_restore_after_death() for the full reasoning.

DSR only (use_adaptive_rewind): if a death-restore is followed by another
death within DEATH_RESTORE_ESCALATION_WINDOW_SECONDS, the snapshot that
was just restored apparently wasn't actually safe (e.g. it captured you
mid-fall off a cliff, not before the fall started) -- so the next restore
skips one snapshot further back than last time, repeating until one
sticks. It resets to the most recent clean snapshot once a restore goes
unchallenged for that long, so an ordinary one-off death (e.g. mid-fight)
loses minimal progress. Elden Ring does not use this -- it always
restores the single most recent clean snapshot, unchanged from before.

Every restore is verified via copy_and_verify() (MD5 compare + retry).
Snapshots numbered higher than the baseline recorded in a per-game state
file (the highest Kopie number that already existed the very first time
the watcher ever ran here for that game) are considered "ours" and
pruned oldest-first beyond KOPIE_RETENTION_COUNT (_prune_old_kopies());
that baseline is persisted to disk specifically so pruning still works
correctly across restarts -- an in-memory-only list would forget
everything and need 30 fresh snapshots before pruning could resume each
time the process restarts, which in practice is often (the watcher
doesn't survive game restarts, crashes, or a PC reboot). Files at or
below the baseline (manual backups predating the watcher) are never
touched.

Usage (Windows, Python 3 installed):
    python er_save_watcher.py [-g {er,dsr}] [save_dir]

    save_dir is the folder containing the save file. If omitted, it's
    auto-detected (see GAME_PROFILES' default_save_dir) as long as
    there's exactly one candidate SteamID subfolder; otherwise required.

Overlay limitations:
    The overlay is a normal always-on-top desktop window. It will show up
    fine over the game running in Borderless Windowed mode. True
    exclusive Fullscreen mode in DirectX can paint over any external
    topmost window -- that is a Windows/DirectX limitation no external
    script can bypass. If the overlay does not appear over the game,
    switch the game's display mode to Borderless/Windowed.

Controls:
    Drag the overlay's title bar to reposition it.
    Click the "x" button to close the overlay (the watcher and its log
    keep running until the process exits).
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

ER_SLOT0_DATA_START = 0x310
ER_GA_ITEM_COUNT = 0x1400  # number of GaItem slots preceding PlayerGameData, per ER-Save-Editor's SaveSlot::read()
ER_HEALTH_REL_OFFSET = 8  # byte offset of `health` within PlayerGameData, per its struct field order
ER_SOULS_REL_OFFSET = 100  # byte offset of `souls` (= runes) within PlayerGameData, per its struct field order
ER_READ_PREFIX_SIZE = 0x40000  # comfortably covers the header + worst-case ga_items array + PlayerGameData header

# ---- Dark Souls Remastered specifics ------------------------------------

DSR_AES_KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")
DSR_BND4_HEADER_LEN = 64
DSR_BND4_ENTRY_HEADER_LEN = 32
DSR_BND4_ENTRY_MAGIC = b"\x50\x00\x00\x00\xff\xff\xff\xff"
DSR_HEALTH_REL_OFFSET = 96  # verified against two real characters' current/max HP
DSR_SOULS_REL_OFFSET = 224  # current (spendable) souls -- verified against a live in-game value of 8000.
DSR_SOULS_MEMORY_REL_OFFSET = 228  # lifetime cumulative souls; monotonically non-decreasing, never resets on death/spending.
# 228 (souls_givifier's documented "set both 224 and 228" target) was used here originally and seemed to verify
# correctly at the time -- but that was a coincidence: souls_memory only diverges from current souls once you
# actually spend or lose some, which hadn't happened yet in those earlier checks. Using souls_memory for death
# detection meant souls never read 0 after a death (it only ever grows), so the restore never fired.

# ---- Per-game profiles ---------------------------------------------------


def _find_default_subfolder_dir(base):
    """Shared helper: returns base/<the one digit-named subfolder>, or None if there's zero or more than one."""
    if not os.path.isdir(base):
        return None
    candidates = [d for d in os.listdir(base) if d.isdigit() and os.path.isdir(os.path.join(base, d))]
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


GAME_PROFILES = {
    "er": {
        "save_filename": "ER0000.sl2",
        "find_default_save_dir": _find_default_save_dir_er,
    },
    "dsr": {
        "save_filename": "DRAKS0005.sl2",
        "find_default_save_dir": _find_default_save_dir_dsr,
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


def read_vitals_er(path):
    """Returns (health, souls) for the Elden Ring save at path, or (None, None) on any read failure."""
    try:
        with open(path, "rb") as f:
            f.seek(ER_SLOT0_DATA_START)
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


def _dsr_decrypt_entry(raw, size, data_offset):
    """
    Decrypts one DSR BND4 entry with AES-128-CBC (key is published/well-known,
    not a secret -- see jtesta/souls_givifier). The IV is the first 16 bytes
    at data_offset; the true (unpadded) length is a 4-byte int right after
    decryption starts, followed by that many bytes of actual save data.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # imported lazily: optional dep, only needed for DSR

    iv = raw[data_offset + 16:data_offset + 32]
    encrypted = raw[data_offset + 16:data_offset + size]
    decryptor = Cipher(algorithms.AES128(DSR_AES_KEY), modes.CBC(iv)).decryptor()
    decrypted = decryptor.update(encrypted) + decryptor.finalize()
    data_len = struct.unpack_from("<i", decrypted, 16)[0]
    return decrypted[20:20 + data_len]


def _dsr_find_first_occupied_slot(raw, entries):
    """
    Entry #10 (the 11th) holds an occupancy table for the 10 character slots.
    Returns the lowest occupied slot index, or None if none are occupied.
    Only the first one is ever tracked -- if you play multiple DSR
    characters in the same save, only the lowest-numbered slot is watched.
    """
    size, data_offset = entries[10]
    occupancy_data = _dsr_decrypt_entry(raw, size, data_offset)
    slot_bytes = occupancy_data[176:186]
    for i in range(10):
        if slot_bytes[i:i + 1] != b"\x00":
            return i
    return None


def read_vitals_dsr(path):
    """Returns (health, souls) for the DSR save at path, or (None, None) on any read failure."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, None
    try:
        entries = _bnd4_parse_entries(raw)
        slot = _dsr_find_first_occupied_slot(raw, entries)
        if slot is None:
            return None, None
        size, data_offset = entries[slot]
        decrypted = _dsr_decrypt_entry(raw, size, data_offset)
        health = struct.unpack_from("<I", decrypted, DSR_HEALTH_REL_OFFSET)[0]
        souls = struct.unpack_from("<I", decrypted, DSR_SOULS_REL_OFFSET)[0]
        return health, souls
    except (ValueError, struct.error):
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
                 on_death_restore, on_death_restore_failed, on_prune, on_prune_failed,
                 use_adaptive_rewind=False):
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
        self.use_adaptive_rewind = use_adaptive_rewind
        self._last_size = None
        self._last_mtime = None
        self._last_hash = None
        self._death_restore_pending = False
        self._death_detected_monotonic = None
        self._death_restore_skip = 0  # adaptive rewind only: how many extra clean snapshots back to skip
        self._last_death_restore_monotonic = None  # adaptive rewind only: when the last death-restore happened
        self._kopie_baseline = load_or_init_kopie_baseline(self.save_dir, self.save_stem)
        self._stop = threading.Event()

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
        health, souls = self.read_vitals(self.path)
        self.on_change(size, delta, digest[:12], health, souls)
        if delta is None:  # don't snapshot the initial baseline read, only real changes
            return
        if is_clean_state(health, souls):
            self._snapshot_change()

    def _check_death(self):
        health, souls = self.read_vitals(self.path)
        if health is None or souls is None:
            return
        if is_clean_state(health, souls):
            self._death_restore_pending = False
            self._death_detected_monotonic = None
            return
        if self._death_restore_pending:
            return  # already attempted a restore for this unclean episode; wait for it to clear
        if self._death_detected_monotonic is None:
            self._death_detected_monotonic = time.monotonic()
            return  # unclean state just observed; wait out DEATH_RESTORE_DELAY_SECONDS before acting
        if time.monotonic() - self._death_detected_monotonic < DEATH_RESTORE_DELAY_SECONDS:
            return  # still within the delay window; re-checked (and re-armed above) every tick
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
            health, souls = self.read_vitals(candidate)
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

    if args.game == "dsr":
        try:
            import cryptography  # noqa: F401
        except ImportError:
            print(
                "ERROR: DSR support requires the 'cryptography' package, which isn't installed.\n"
                "Install it with:  pip install cryptography",
                file=sys.stderr,
            )
            sys.exit(1)
        read_vitals_fn = read_vitals_dsr
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

    watcher = SaveWatcher(
        save_file, save_stem, read_vitals_fn, on_change, on_snapshot, on_snapshot_failed,
        on_death_restore, on_death_restore_failed, on_prune, on_prune_failed,
        use_adaptive_rewind=(args.game == "dsr"),
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
