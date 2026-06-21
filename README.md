# ER0000.sl2 Save Watcher

A little Windows tool that watches your Elden Ring save file while you play, automatically keeps a history of checkpoints, and can undo a death by restoring your last good checkpoint — all without you having to manually copy files around.

## What it actually does

- **Watches** `ER0000.sl2` (your save file) and notices every time the game writes to it.
- **Takes snapshots**: each time you make real progress (you're alive *and* your runes are accounted for), it saves a numbered copy — `ER0000 - Kopie (12).sl2`, `(13)`, `(14)`, and so on — building up a history you can fall back to.
- **Skips bad moments**: it won't snapshot a state where you're dead, or where you've just respawned but haven't picked your runes back up yet. Those aren't checkpoints worth keeping.
- **Detects death and restores automatically**: if your character's health (and runes) stay at an "unclean" state for a while, it copies the last good checkpoint back over your live save for you.
- **Verifies every restore**: after copying a checkpoint back, it double-checks the copy actually matches byte-for-byte before calling it done.
- **Cleans up after itself**: keeps the most recent 30 snapshots it created and deletes older ones automatically, so it doesn't quietly fill your disk. It never touches files that were already there before it started (including any manual backups you made yourself).
- **Shows a small on-screen overlay** with what it's been doing, and writes a full log to `save_changes.log` next to the program.

## Running it

```
python er_save_watcher.py [save_dir]
```

`save_dir` is the folder containing `ER0000.sl2`. If you don't pass one, it tries to auto-detect it from `%APPDATA%\EldenRing\<your SteamID>\` — this only works automatically if you've only ever played with one Steam account on this PC; otherwise you'll need to pass the path yourself.

## Known limitations

- The on-screen overlay shows up reliably in **Borderless Windowed** mode. True exclusive Fullscreen can paint over it — that's a Windows/DirectX thing, not something this tool can get around.
- While Elden Ring is actively running, *it* holds the real, authoritative copy of your progress in memory — our restore can only "stick" once the game isn't actively fighting it (e.g. you've returned to the title screen). That's why the death-restore waits a while before acting, rather than firing the instant you die.
- This is a heuristic tool poking at an undocumented file format. It's been tested against one save file structure and should be used with that understanding — keep your own backups too.

## Credits

Figuring out *where* in the save file to look took real reverse-engineering work done by other people, not us. This tool wouldn't exist without:

- **[Ariescyn/EldenRing-Save-Manager](https://github.com/Ariescyn/EldenRing-Save-Manager)** — a Python save editor whose code showed us the save file's checksum/slot layout (10 character slots, MD5-checksummed) and the technique for locating the rune count by searching for a known value.
- **[ClayAmore/ER-Save-Editor](https://github.com/ClayAmore/ER-Save-Editor)** — a Rust save editor with an actual structural parser for the save format. Its `PlayerGameData` and `GaItem` struct definitions are what let this tool reliably find the character's health and rune count without guessing offsets, by properly walking the save's variable-length item list the same way the game itself does.

Thank you to both projects for doing the hard part.
