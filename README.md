# .sl2 Save Watcher

A little Windows tool that watches your Elden Ring or Dark Souls Remastered save file while you play, and after you die, automatically restores your last good checkpoint — all without you having to manually copy files around.

## Supported games

| Game | `-g`/`--game` | Save file | Default save folder |
|---|---|---|---|
| Elden Ring | `er` (default) | `ER0000.sl2` | `%APPDATA%\EldenRing\<SteamID>\` |
| Dark Souls Remastered | `dsr` | `DRAKS0005.sl2` | `<Documents>\NBGI\DARK SOULS REMASTERED\<SteamID>\` |

The two use completely different save formats under the hood (Elden Ring's is unencrypted; DSR's is AES-128-CBC encrypted per character slot), but the tool behaves the same way from the outside regardless of which one you pick. DSR additionally needs the `cryptography` Python package installed (see Running it below) — Elden Ring needs nothing extra.

## What it actually does

- **Watches** your save file and notices every time the game writes to it.
- **Takes snapshots**: each time you make real progress (you're alive *and* your runes/souls are accounted for), it saves a numbered copy — `ER0000 - Kopie (12).sl2`, `(13)`, `(14)` (or `DRAKS0005 - Kopie (N).sl2` for DSR), and so on — building up a history you can fall back to.
- **Skips bad moments**: it won't snapshot a state where you're dead, or where you've just respawned but haven't picked your runes/souls back up yet. Those aren't checkpoints worth keeping.
- **Detects death and restores automatically**: if your character's health (and runes/souls) stay at an "unclean" state for a while, it copies the last good checkpoint back over your live save for you. For DSR specifically, if a restore is followed by *another* death shortly after, it assumes that snapshot wasn't actually safe (e.g. it caught you mid-fall off a cliff) and automatically reaches one snapshot further back each time, until one sticks. Elden Ring always uses the single most recent clean snapshot.
- **Verifies every restore**: after copying a checkpoint back, it double-checks the copy actually matches byte-for-byte before calling it done.
- **Cleans up after itself**: keeps the most recent 30 snapshots it created and deletes older ones automatically, so it doesn't quietly fill your disk. It never touches files that were already there before it started (including any manual backups you made yourself).
- **Shows a small on-screen overlay** with what it's been doing, and writes a full log (including live health/souls readings, for debugging) to `save_changes.log` next to the program.
- **Plays two different sounds** so you can tell what just happened without looking at the overlay: a short single beep when a new snapshot is taken, and a longer two-tone beep when a checkpoint has actually been restored.

## What to do after you die

1. Die as normal and let the death animation play out.
2. From the death screen, go back to the **main menu** instead of continuing to play — don't keep playing on the spot, since the game can overwrite the restore again while you're actively still in the session.
3. Wait. The tool needs your health/runes (or souls, for DSR) to stay in an "unclean" state for a little while before it acts, so the restore doesn't happen instantly.
4. Listen for the **two-tone restore sound**. That's your signal that the last good checkpoint has been copied back over your save.
5. Only now click **Continue**. Loading before the sound plays means you'd load the version of the save before the restore happened.

## Running it

There are two ways to run this:

### Option A: standalone .exe (no Python needed)

Just double-click `ER_Save_Watcher.exe`, or run it from a terminal if you want to pass options explicitly:

```
ER_Save_Watcher.exe [-g {er,dsr}] [save_dir]
```

### Option B: run the Python script directly

Requirements:
- **Python 3** installed on Windows, including **tkinter** (bundled by default with the official python.org installer — just don't deselect it during setup).
- For Elden Ring: no `pip install` of anything else — only Python's standard library is used.
- For Dark Souls Remastered: also run `pip install cryptography` once (needed to decrypt DSR's save slots; not required at all for Elden Ring).

```
python er_save_watcher.py [-g {er,dsr}] [save_dir]
```

### Options

- `-g`/`--game` picks the save format: `er` (Elden Ring, the default) or `dsr` (Dark Souls Remastered).
- `save_dir` is the folder containing the save file. If you don't pass one, it tries to auto-detect it (see the table above) — this only works automatically if you've only ever played with one Steam account on this PC; otherwise you'll need to pass the path yourself, e.g.:

```
ER_Save_Watcher.exe -g er "C:\Users\<you>\AppData\Roaming\EldenRing\<your SteamID>"
ER_Save_Watcher.exe -g dsr "C:\Users\<you>\Documents\NBGI\DARK SOULS REMASTERED\<your SteamID>"
```

No admin rights are needed either way — it only reads/writes your own save folder and writes its log next to wherever it's run from. If you have more than one save folder for the same game (e.g. two Steam accounts, or DSR's PS-import slot), each gets its own pruning/state tracking automatically — just point the tool at the right folder.

### Building the .exe yourself

If you change the script and want to rebuild the `.exe`:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name ER_Save_Watcher er_save_watcher.py
```

The result lands in `dist\ER_Save_Watcher.exe`.

## Known limitations

- The on-screen overlay shows up reliably in **Borderless Windowed** mode. True exclusive Fullscreen can paint over it — that's a Windows/DirectX thing, not something this tool can get around.
- While the game is actively running, *it* holds the real, authoritative copy of your progress in memory — our restore can only "stick" once the game isn't actively fighting it (e.g. you've returned to the title screen). That's why the death-restore waits a while before acting, rather than firing the instant you die.
- For DSR, only the **first occupied character slot** is tracked. If you have more than one character in the same save and play a different one, the tool is watching the wrong character. There's no slot-picker yet.
- This is a heuristic tool poking at undocumented file formats. It's been tested against one save file structure per game and should be used with that understanding — keep your own backups too.

## Credits

Figuring out *where* in the save file to look took real reverse-engineering work done by other people, not us. This tool wouldn't exist without:

- **[Ariescyn/EldenRing-Save-Manager](https://github.com/Ariescyn/EldenRing-Save-Manager)** — a Python save editor whose code showed us the Elden Ring save file's checksum/slot layout (10 character slots, MD5-checksummed) and the technique for locating the rune count by searching for a known value.
- **[ClayAmore/ER-Save-Editor](https://github.com/ClayAmore/ER-Save-Editor)** — a Rust save editor with an actual structural parser for the Elden Ring save format. Its `PlayerGameData` and `GaItem` struct definitions are what let this tool reliably find the character's health and rune count without guessing offsets, by properly walking the save's variable-length item list the same way the game itself does.
- **[jtesta/souls_givifier](https://github.com/jtesta/souls_givifier)** — showed us the generic BND4 container format shared across Dark Souls/Elden Ring saves, and the AES-128-CBC key and decryption scheme for Dark Souls Remastered (and DS2/DS3, unused here so far).
- **[tarvitz/dsfp](https://github.com/tarvitz/dsfp)** — a documented field-offset table for original (unencrypted) Dark Souls that helped narrow down where to look in DSR's decrypted slot data, even though DSR's actual layout had shifted slightly from it.

Thank you to all four projects for doing the hard part.
