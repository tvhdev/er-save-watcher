# .sl2 Save Watcher

A little Windows tool that watches your Elden Ring, Dark Souls Remastered, Dark Souls III, or Dark Souls II: Scholar of the First Sin save file while you play, and after you die, automatically restores your last good checkpoint with acoustic notification. This way you continue where you died and you save a lot of time because you don't need to run to the place you died to pickup your runes!

## Supported games

| Game | `-g`/`--game` | Save file | Default save folder |
|---|---|---|---|
| Elden Ring | `er` (default) | `ER0000.sl2` | `%APPDATA%\EldenRing\<SteamID>\` |
| Dark Souls Remastered | `dsr` | `DRAKS0005.sl2` | `<Documents>\NBGI\DARK SOULS REMASTERED\<SteamID>\` |
| Dark Souls III | `ds3` | `DS30000.sl2` | `%APPDATA%\DarkSoulsIII\<id>\` |
| Dark Souls II: Scholar of the First Sin | `ds2` | `DS2SOFS0000.sl2` | `%APPDATA%\DarkSoulsII\<id>\` |

These use completely different save formats under the hood (Elden Ring's is unencrypted; DSR's, DS3's, and DS2's are AES-128-CBC encrypted per character slot, each with its own key), but the tool behaves the same way from the outside regardless of which one you pick. DSR, DS3, and DS2 additionally need the `cryptography` Python package installed (see Running it below) — Elden Ring needs nothing extra.

## What it actually does

- **Watches** your save file and notices every time the game writes to it.
- **Takes snapshots**: each time you make real progress (you're alive *and* your runes/souls are accounted for), it saves a numbered copy — `ER0000 - Kopie (12).sl2`, `(13)`, `(14)` (or `DRAKS0005 - Kopie (N).sl2` for DSR), and so on — building up a history you can fall back to.
- **Skips bad moments**: it won't snapshot a state where you're dead, or where you've just respawned but haven't picked your runes/souls back up yet. Those aren't checkpoints worth keeping.
- **Detects death and restores automatically**: when your save reads "unclean" (you died / lost your souls), it restores the most recent clean checkpoint — but only once the save file goes quiet (stops being written for ~15 seconds), i.e. once you've quit to the **main menu**. A restore done while the game is still running would just be overwritten by its next autosave; done at the menu it sticks, because the game reloads from disk when you next hit Continue.
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
ER_Save_Watcher.exe [-g {er,dsr,ds3,ds2}] [-s SLOT] [save_dir]
```

### Option B: run the Python script directly

Requirements:
- **Python 3** installed on Windows, including **tkinter** (bundled by default with the official python.org installer — just don't deselect it during setup).
- For Elden Ring: no `pip install` of anything else — only Python's standard library is used.
- For Dark Souls Remastered, Dark Souls III, or Dark Souls II: also run `pip install cryptography` once (needed to decrypt their save slots; not required at all for Elden Ring).

```
python er_save_watcher.py [-g {er,dsr,ds3,ds2}] [-s SLOT] [save_dir]
```

### Options

- `-g`/`--game` picks the save format: `er` (Elden Ring, the default), `dsr` (Dark Souls Remastered), `ds3` (Dark Souls III), or `ds2` (Dark Souls II: Scholar of the First Sin).
- `save_dir` is the folder containing the save file. If you don't pass one, it tries to auto-detect it (see the table above) — this only works automatically if you've only ever played with one Steam account on this PC; otherwise you'll need to pass the path yourself, e.g.:

```
ER_Save_Watcher.exe -g er "C:\Users\<you>\AppData\Roaming\EldenRing\<your SteamID>"
ER_Save_Watcher.exe -g dsr "C:\Users\<you>\Documents\NBGI\DARK SOULS REMASTERED\<your SteamID>"
ER_Save_Watcher.exe -g ds3 "C:\Users\<you>\AppData\Roaming\DarkSoulsIII\<your id>"
ER_Save_Watcher.exe -g ds2 "C:\Users\<you>\AppData\Roaming\DarkSoulsII\<your id>"
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
- If you have **multiple characters** in one save, the tool auto-detects which one you're playing (the only character whose save slot changes when the game saves) and watches that one. You can also force a specific slot with `-s`/`--slot N` if auto-detection ever picks wrong.
- DS3's health/souls offsets weren't independently re-verified against two real characters the way DSR's were (see the module docstring) — they're sourced from a working, actively-used save editor, but carry somewhat lower confidence than DSR's.
- For DS2, death detection runs on **souls only** (confirmed: souls drops to 0 on death and was verified against a live in-game value). The HP it shows for DS2 is the character's *base* max HP read from the save and is **informational only** — it doesn't include the bonuses HP-boosting rings apply in-game, so it can read lower than the number on your screen when such a ring is equipped. (DS2 also only saves after you respawn at a bonfire, i.e. at full HP, so its saved HP never reaches 0 anyway — which is exactly why souls, not HP, is the death signal there.)
- This is a heuristic tool poking at undocumented file formats. It's been tested against one save file structure per game and should be used with that understanding — keep your own backups too.

## Credits

Figuring out *where* in the save file to look took real reverse-engineering work done by other people, not us. This tool wouldn't exist without:

- **[Ariescyn/EldenRing-Save-Manager](https://github.com/Ariescyn/EldenRing-Save-Manager)** — a Python save editor whose code showed us the Elden Ring save file's checksum/slot layout (10 character slots, MD5-checksummed) and the technique for locating the rune count by searching for a known value.
- **[ClayAmore/ER-Save-Editor](https://github.com/ClayAmore/ER-Save-Editor)** — a Rust save editor with an actual structural parser for the Elden Ring save format. Its `PlayerGameData` and `GaItem` struct definitions are what let this tool reliably find the character's health and rune count without guessing offsets, by properly walking the save's variable-length item list the same way the game itself does.
- **[jtesta/souls_givifier](https://github.com/jtesta/souls_givifier)** — showed us the generic BND4 container format shared across Dark Souls/Elden Ring saves, and the AES-128-CBC keys and decryption scheme for Dark Souls Remastered, Dark Souls III, and Dark Souls II: Scholar of the First Sin (including DS2's distinct layout, where the slot occupancy/name table lives in the first container entry and each character's data is one entry further along).
- **[tarvitz/dsfp](https://github.com/tarvitz/dsfp)** — a documented field-offset table for original (unencrypted) Dark Souls that helped narrow down where to look in DSR's decrypted slot data, even though DSR's actual layout had shifted slightly from it.
- **[alfizari/Dark-Souls-3-Save-Editor-PS4-PC](https://github.com/alfizari/Dark-Souls-3-Save-Editor-PS4-PC)** — a full DS3 save editor whose item-array-walking technique and health/souls offsets (relative to the end of that array) made DS3 support possible, the same general approach as Elden Ring's variable-length layout problem.

Thank you to all five projects for doing the hard part.
