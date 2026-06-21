# ER0000.sl2 Save Watcher

A little Windows tool that watches your Elden Ring save file while you play, and after your die, automatically restores your last good checkpoint — all without you having to manually copy files around.

## What it actually does

- **Watches** `ER0000.sl2` (your save file) and notices every time the game writes to it.
- **Takes snapshots**: each time you make real progress (you're alive *and* your runes are accounted for), it saves a numbered copy — `ER0000 - Kopie (12).sl2`, `(13)`, `(14)`, and so on — building up a history you can fall back to.
- **Skips bad moments**: it won't snapshot a state where you're dead, or where you've just respawned but haven't picked your runes back up yet. Those aren't checkpoints worth keeping.
- **Detects death and restores automatically**: if your character's health (and runes) stay at an "unclean" state for a while, it copies the last good checkpoint back over your live save for you.
- **Verifies every restore**: after copying a checkpoint back, it double-checks the copy actually matches byte-for-byte before calling it done.
- **Cleans up after itself**: keeps the most recent 30 snapshots it created and deletes older ones automatically, so it doesn't quietly fill your disk. It never touches files that were already there before it started (including any manual backups you made yourself).
- **Shows a small on-screen overlay** with what it's been doing, and writes a full log to `save_changes.log` next to the program.
- **Plays two different sounds** so you can tell what just happened without looking at the overlay: a short single beep when a new snapshot is taken, and a longer two-tone beep when a checkpoint has actually been restored.

## What to do after you die

1. Die as normal and let the death animation play out.
2. From the death screen, go back to the **main menu** instead of continuing to play — don't keep playing on the spot, since the game can overwrite the restore again while you're actively still in the session.
3. Wait. The tool needs your health/runes to stay in an "unclean" state for a little while before it acts, so the restore doesn't happen instantly.
4. Listen for the **two-tone restore sound**. That's your signal that the last good checkpoint has been copied back over your save.
5. Only now click **Continue**. Loading before the sound plays means you'd load the version of the save before the restore happened.

## Running it

There are two ways to run this:

### Option A: standalone .exe (no Python needed)

Just double-click `ER_Save_Watcher.exe`, or run it from a terminal if you want to pass a save folder explicitly:

```
ER_Save_Watcher.exe [save_dir]
```

### Option B: run the Python script directly

Requirements:
- **Python 3** installed on Windows, including **tkinter** (bundled by default with the official python.org installer — just don't deselect it during setup).
- No `pip install` of anything else — the script only uses Python's standard library (`tkinter`, `hashlib`, `struct`, `winsound`, etc.).

```
python er_save_watcher.py [save_dir]
```

### The save_dir argument (both options)

`save_dir` is the folder containing `ER0000.sl2`. If you don't pass one, it tries to auto-detect it from `%APPDATA%\EldenRing\<your SteamID>\` — this only works automatically if you've only ever played with one Steam account on this PC; otherwise you'll need to pass the path yourself, e.g.:

```
ER_Save_Watcher.exe "C:\Users\<you>\AppData\Roaming\EldenRing\<your SteamID>"
```

No admin rights are needed either way — it only reads/writes your own save folder and writes its log next to wherever it's run from.

### Building the .exe yourself

If you change the script and want to rebuild the `.exe`:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name ER_Save_Watcher er_save_watcher.py
```

The result lands in `dist\ER_Save_Watcher.exe`.

## Known limitations

- The on-screen overlay shows up reliably in **Borderless Windowed** mode. True exclusive Fullscreen can paint over it — that's a Windows/DirectX thing, not something this tool can get around.
- While Elden Ring is actively running, *it* holds the real, authoritative copy of your progress in memory — our restore can only "stick" once the game isn't actively fighting it (e.g. you've returned to the title screen). That's why the death-restore waits a while before acting, rather than firing the instant you die.
- This is a heuristic tool poking at an undocumented file format. It's been tested against one save file structure and should be used with that understanding — keep your own backups too.

## Credits

Figuring out *where* in the save file to look took real reverse-engineering work done by other people, not us. This tool wouldn't exist without:

- **[Ariescyn/EldenRing-Save-Manager](https://github.com/Ariescyn/EldenRing-Save-Manager)** — a Python save editor whose code showed us the save file's checksum/slot layout (10 character slots, MD5-checksummed) and the technique for locating the rune count by searching for a known value.
- **[ClayAmore/ER-Save-Editor](https://github.com/ClayAmore/ER-Save-Editor)** — a Rust save editor with an actual structural parser for the save format. Its `PlayerGameData` and `GaItem` struct definitions are what let this tool reliably find the character's health and rune count without guessing offsets, by properly walking the save's variable-length item list the same way the game itself does.

Thank you to both projects for doing the hard part.
