# zedit

Save editor for the Steam release of ZENONIA 1 (Com2uS / CFK, appid 4538960,
ver 1.0.0_10004). Single-file Windows `.exe`, no installer, no runtime
dependencies — runs under Wine (tested target: GameHub on Android).

## What it does

- **Browse** opens a file picker; point it at any `.bin` save.
- **ItemData.bin** (`SVID`): edit gold (capped at 999,999,999 and written to
  every item slot, since the game replicates it there), plus a read-only list
  of the items in the save.
- **AchivementData.bin** (`SVAG`): a scrollable grid of 255 cells, one per
  internal achievement id (0–254). Click to toggle locked/unlocked, with a
  live unlocked count. Useful for probing which ids map to which of the 44
  real achievements.
- **Save** writes the file, after first copying the original to `<name>.bak`.
  An existing backup is never overwritten, so the first backup always survives.
- Anything else — `UserData.bin`, `SettingData.bin`, corrupted files,
  non-saves — is refused with a message saying what was found.

Both CRC32 checksums in the SV2S container are recomputed on save, inner
first, then outer (the outer covers the inner). A file whose checksums do not
validate is rejected on load rather than opened.

## Getting the exe

Every push builds `zedit.exe` on a Windows runner via GitHub Actions
(`.github/workflows/build.yml`); download it from the workflow run's
`zedit-windows` artifact.

To build it yourself, on Windows (or with Windows Python inside a Wine
container):

```
pip install pyinstaller
pyinstaller --onefile --windowed zedit.py
```

The result is `dist\zedit.exe`.

## Running from source

Python 3.8+ with tkinter (bundled with the standard Windows installer):

```
python zedit.py
```

## Tests

```
python -m unittest test_zedit -v
```

The suite builds synthetic saves in the confirmed format and checks that an
unmodified load/save round trip is byte-identical, that a gold-only edit
touches exactly the gold field in every slot plus the two CRC fields, that
corrupted checksums are rejected, and that backups are never clobbered.
After building, it is worth repeating the round-trip check once against a
real save: load it, save with no changes, and diff against the original.

## File format notes

Every save is an SV2S container (little-endian):

| Offset | Size | Meaning |
|---|---|---|
| 0x00 | 4 | magic `SV2S` |
| 0x04 | 4 | uint32 length of bytes from 0x0C (filesize − 12) |
| 0x08 | 4 | uint32 CRC32 of bytes from 0x0C |
| 0x0C | 4 | chunk id (`SVID` items, `SVAG` achievement flags, `SVPD` character, `SVAD` progress counters, `SVAR` reward claims, `SVSD` settings) |
| 0x10 | 4 | uint32 length of bytes from 0x18 (filesize − 24) |
| 0x14 | 4 | uint32 CRC32 of bytes from 0x18 |
| 0x18 | 4 | uint32 format version (observed: 1) |
| 0x1C | … | payload |

`SVID` payload: 38-byte item slots (a 7704-byte file holds 202). Slot layout:
uint32 gold (replicated in every slot), uint32 item id (`0xFFFFFFFF` empty),
uint8 durability, uint8 max durability, uint8 stack, uint8 attached sub-item
slot (`0xFF` none), uint8 unidentified flag, uint8 rarity, then four modifier
pairs of uint16 id + uint32 value (`0xFFFF` id = unused; percentages are
stored ×100). Slots 0–9 are equipment, 10+ are bag pages.

`SVAG` payload: 510 bytes — 255 entries of 2 bytes, `00 00` locked, `01 01`
unlocked. Entry number is an internal id, not the in-game list position.
