#!/usr/bin/env python3
r"""
ER0000.sl2 Save File Watcher + Auto-Restore + On-Screen Overlay.

Watches the Elden Ring save file, snapshots it into numbered
"ER0000 - Kopie (N).sl2" files whenever the state is "clean" (health > 0
and souls > 0 -- see is_clean_state()), and can restore an earlier
snapshot back over the live save. Pre-existing Kopie files and
ER0000-backup.sl2 are never renamed, overwritten, or deleted.

Restore path: if health/souls go unclean and stay that way for
DEATH_RESTORE_DELAY_SECONDS, restores the latest clean snapshot. The
delay matters because Elden Ring holds authoritative state in memory
while running and can overwrite our restore with its own next autosave
-- see _check_death()/_restore_after_death() for the full reasoning.

Health/souls are located by parsing the save structurally (header, the
variable-length ga_items array, then PlayerGameData) rather than via a
fixed offset, since item changes shift everything after that array --
see _find_player_game_data_start(). Every restore is verified via
copy_and_verify() (MD5 compare + retry). Snapshots numbered higher than
the baseline recorded in WATCHER_STATE_FILE (the highest Kopie number
that already existed the very first time the watcher ever ran here) are
considered "ours" and pruned oldest-first beyond KOPIE_RETENTION_COUNT
(_prune_old_kopies()); that baseline is persisted to disk specifically so
pruning still works correctly across restarts -- an in-memory-only list
would forget everything and need 30 fresh snapshots before pruning could
resume each time the process restarts, which in practice is often (the
watcher doesn't survive game restarts, crashes, or a PC reboot). Files at
or below the baseline (manual backups predating the watcher) are never
touched.

Usage (Windows, Python 3 installed):
    python er_save_watcher.py [save_dir]

    save_dir is the EldenRing save folder containing ER0000.sl2. If omitted,
    it's auto-detected from %APPDATA%\EldenRing\<SteamID64>\ as long as
    there's exactly one SteamID subfolder there; otherwise it's required.

Overlay limitations:
    The overlay is a normal always-on-top desktop window. It will show up
    fine over Elden Ring running in Borderless Windowed mode. True
    exclusive Fullscreen mode in DirectX can paint over any external
    topmost window -- that is a Windows/DirectX limitation no external
    script can bypass. If the overlay does not appear over the game,
    switch Elden Ring's display mode to Borderless/Windowed.

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

KOPIE_NAME_RE = re.compile(r"^ER0000 - Kopie(?: \((\d+)\))?\.sl2$")
# sys.executable is the .exe itself when frozen by PyInstaller; __file__ would
# otherwise resolve to a temp extraction folder that's deleted on exit.
_APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_APP_DIR, "save_changes.log")
WATCHER_STATE_FILE = os.path.join(_APP_DIR, "watcher_state.txt")
POLL_INTERVAL_SECONDS = 1.0
MAX_EVENTS_SHOWN = 8
DEATH_RESTORE_DELAY_SECONDS = 15
RESTORE_VERIFY_MAX_ATTEMPTS = 5
KOPIE_RETENTION_COUNT = 30  # how many watcher-created snapshots to keep before pruning the oldest; see _prune_old_kopies()
ENABLE_DEATH_RESTORE = True
SLOT0_DATA_START = 0x310
GA_ITEM_COUNT = 0x1400  # number of GaItem slots preceding PlayerGameData, per ER-Save-Editor's SaveSlot::read()
HEALTH_REL_OFFSET = 8  # byte offset of `health` within PlayerGameData, per its struct field order
SOULS_REL_OFFSET = 100  # byte offset of `souls` (= runes) within PlayerGameData, per its struct field order
READ_PREFIX_SIZE = 0x40000  # comfortably covers the header + worst-case ga_items array + PlayerGameData header


def find_default_save_dir():
    """
    EldenRing saves live at %APPDATA%\\EldenRing\\<SteamID64>\\. The SteamID64
    part can't be known generically, but most machines have only ever had one
    Steam account play, so auto-pick it if there's exactly one such subfolder.
    Returns None if APPDATA isn't set, the EldenRing folder doesn't exist, or
    there's more than one (or zero) candidate subfolders -- the caller must
    then require an explicit save_dir argument.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = os.path.join(appdata, "EldenRing")
    if not os.path.isdir(base):
        return None
    candidates = [d for d in os.listdir(base) if d.isdigit() and os.path.isdir(os.path.join(base, d))]
    if len(candidates) == 1:
        return os.path.join(base, candidates[0])
    return None


def _find_player_game_data_start(buf):
    """
    Walks the variable-length ga_items array (each entry is 8, 16, or 25 bytes
    depending on item type) to find where PlayerGameData begins. There is no
    fixed offset for this -- it shifts whenever held/equipped items change.
    Mirrors GaItem::read() in ClayAmore/ER-Save-Editor's save_slot.rs.
    """
    pos = 4 + 4 + 0x18  # ver + map_id + unknown padding, relative to slot start
    for _ in range(GA_ITEM_COUNT):
        item_id = struct.unpack_from("<I", buf, pos + 4)[0]
        pos += 8  # gaitem_handle + item_id
        high_nibble = item_id & 0xF0000000
        if item_id != 0 and high_nibble == 0:
            pos += 4 + 4 + 4 + 1  # weapon-type GaItem
        elif item_id != 0 and high_nibble == 0x10000000:
            pos += 4 + 4  # armor-type GaItem
    return pos


def read_vitals(path):
    """Returns (health, souls) for the save at path, or (None, None) on any read failure."""
    try:
        with open(path, "rb") as f:
            f.seek(SLOT0_DATA_START)
            buf = f.read(READ_PREFIX_SIZE)
    except OSError:
        return None, None
    try:
        player_game_data_start = _find_player_game_data_start(buf)
        health = struct.unpack_from("<I", buf, player_game_data_start + HEALTH_REL_OFFSET)[0]
        souls = struct.unpack_from("<I", buf, player_game_data_start + SOULS_REL_OFFSET)[0]
        return health, souls
    except struct.error:
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


def list_numbered_kopies(save_dir):
    """Return [(number, path), ...] for files matching the Kopie naming scheme, newest (highest number) first."""
    found = []
    for name in os.listdir(save_dir):
        match = KOPIE_NAME_RE.match(name)
        if match:
            number = int(match.group(1)) if match.group(1) else 1
            found.append((number, os.path.join(save_dir, name)))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return found


def next_kopie_path(save_dir):
    existing = list_numbered_kopies(save_dir)
    next_number = existing[0][0] + 1 if existing else 1
    name = "ER0000 - Kopie.sl2" if next_number == 1 else f"ER0000 - Kopie ({next_number}).sl2"
    return os.path.join(save_dir, name)


def load_or_init_kopie_baseline(save_dir):
    """
    Returns the Kopie number at or below which files are pre-existing (manual
    backups predating the watcher, never touched) and above which they were
    created by the watcher (eligible for pruning). Persisted to
    WATCHER_STATE_FILE on first ever run so this distinction survives process
    restarts -- without it, pruning would have no memory of which files it
    already created and would never catch up.
    """
    try:
        with open(WATCHER_STATE_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        pass
    existing = list_numbered_kopies(save_dir)
    baseline = existing[0][0] if existing else 0
    try:
        with open(WATCHER_STATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(baseline))
    except OSError:
        pass
    return baseline

# ---- File watcher ------------------------------------------------------


class SaveWatcher:
    def __init__(self, path, on_change, on_snapshot, on_snapshot_failed,
                 on_death_restore, on_death_restore_failed, on_prune, on_prune_failed):
        self.path = path
        self.save_dir = os.path.dirname(path)
        self.on_change = on_change
        self.on_snapshot = on_snapshot
        self.on_snapshot_failed = on_snapshot_failed
        self.on_death_restore = on_death_restore
        self.on_death_restore_failed = on_death_restore_failed
        self.on_prune = on_prune
        self.on_prune_failed = on_prune_failed
        self._last_size = None
        self._last_mtime = None
        self._last_hash = None
        self._death_restore_pending = False
        self._death_detected_monotonic = None
        self._kopie_baseline = load_or_init_kopie_baseline(self.save_dir)
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
        self.on_change(size, delta, digest[:12])
        if delta is None:  # don't snapshot the initial baseline read, only real changes
            return
        health, souls = read_vitals(self.path)
        if is_clean_state(health, souls):
            self._snapshot_change()

    def _check_death(self):
        health, souls = read_vitals(self.path)
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
            dest = next_kopie_path(self.save_dir)
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
            (number, p) for number, p in list_numbered_kopies(self.save_dir) if number > self._kopie_baseline
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
            kopies = list_numbered_kopies(self.save_dir)
        except OSError as exc:
            self.on_death_restore_failed(str(exc))
            return
        source = None
        for _, candidate in kopies:  # newest first; skip any that aren't a clean (alive + runes reclaimed) state
            health, souls = read_vitals(candidate)
            if health is not None and souls is not None and health != 0 and souls != 0:
                source = candidate
                break
        if source is None:
            self.on_death_restore_failed("no clean (health > 0 and souls > 0) snapshot found to restore from")
            return
        ok, exc = copy_and_verify(source, self.path)  # one-way copy; source file is never renamed/moved/deleted
        if not ok:
            reason = str(exc) if exc else f"MD5 of restored file never matched source after {RESTORE_VERIFY_MAX_ATTEMPTS} attempts"
            self.on_death_restore_failed(reason)
            return
        stat = os.stat(self.path)
        self._last_size, self._last_mtime = stat.st_size, stat.st_mtime
        self._last_hash = self._hash_file(self.path)
        self.on_death_restore(os.path.basename(source), trigger_health, trigger_souls)

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


def log_change(size, delta, short_hash):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    delta_str = "first snapshot" if delta is None else f"{delta:+d} bytes"
    line = f"[{timestamp}] size={size} ({delta_str}) hash={short_hash}"
    _write_log(line)
    return f"{timestamp}  {delta_str}  #{short_hash}"


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


def log_death_restore(source_name, health, souls):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] UNCLEAN STATE (health={health}, souls={souls}) -- RESTORED '{source_name}' -> live save"
    _write_log(line)
    return f"{timestamp}  RESTORED '{source_name}' (was health={health}, souls={souls})"


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
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.85)
        self.root.configure(bg="black")
        self.root.geometry("+40+40")

        header = tk.Frame(self.root, bg="#202020")
        header.pack(fill="x")
        tk.Label(
            header, text="ER0000.sl2 Watcher", fg="white", bg="#202020",
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
    auto_detected = find_default_save_dir()
    parser = argparse.ArgumentParser(description="ER0000.sl2 save watcher with auto-restore and on-screen overlay.")
    parser.add_argument(
        "save_dir", nargs="?", default=auto_detected,
        help="Path to the EldenRing save folder containing ER0000.sl2"
             + (f" (default, auto-detected: {auto_detected})" if auto_detected else
                " (required: couldn't auto-detect a unique folder under %APPDATA%\\EldenRing)"),
    )
    args = parser.parse_args()
    if args.save_dir is None:
        parser.error(
            "no save_dir given and couldn't auto-detect one under %APPDATA%\\EldenRing "
            "(either it doesn't exist, or there's more than one SteamID subfolder there) -- "
            "pass the save folder path explicitly."
        )
    return args


def main():
    args = parse_args()
    save_file = os.path.join(args.save_dir, "ER0000.sl2")

    overlay = Overlay()
    pending = deque()
    pending_lock = threading.Lock()

    def on_change(size, delta, short_hash):
        line = log_change(size, delta, short_hash)
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

    def on_death_restore(source_name, health, souls):
        line = log_death_restore(source_name, health, souls)
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
        save_file, on_change, on_snapshot, on_snapshot_failed,
        on_death_restore, on_death_restore_failed, on_prune, on_prune_failed,
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
