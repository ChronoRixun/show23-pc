# Builder and launcher

The builder is the **Set up** flow in the Windows launcher. This page covers what it does with the player's game. See [`docs/launcher.md`](../docs/launcher.md) for updates and mods.

## What the player does

1. Extract the native portable ZIP into a new folder, run `show23-pc.exe` and choose **Set up**.
2. Select the 1.00 base game as a package or an unpacked folder. Optionally add the 1.21 update in the same form for card pictures.
3. Choose a fresh install folder. The launcher checks the input, verifies any unpacked files and stages the bundled engine and server into the install. A cancelled or failed setup can be run again.
4. Press **Play**. The first launch prepares the base game files and can take a few minutes. The launcher generates the install's certificate authority if needed, starts the local server and waits until it is ready, then starts the native engine. The local server stops when the game exits.

## Checks before anything is unpacked or started

- The package or folder is the supported title (`CUSA35521`), base game for the base input, update for the update input.
- The base game is version 1.00 and plays as 1.00. If supplied, the update is version 1.21.
- The game executable matches a known checksum.
- There is enough free space, and no file would be written outside the staging folder.

## Install layout

```text
<install>/
  runtime/<release>/emulator/        native game engine for this release
  runtime/<release>/server/          the local server
  runtime/<release>/tools/profile/   game profile
  manifests/                         game and active runtime records
  data/games/CUSA35521/game.json     recorded locations of the game folders
  data/games/CUSA35521/base/         the base game, if unpacked during setup
  data/games/CUSA35521/update/       the update, if unpacked during setup
  data/engine-cache/CUSA35521/       prepared boot and content files
  data/profiles/default/engine/     game saves and settings
  data/profiles/default/server/      server state, this install's certificates
  data/import-cache/                 optional website import
  data/logs/                        preparation, engine, server and import logs
```

`emulator/` is the retained component directory name for the native engine. The portable launcher sits in the separate ZIP extraction folder. Back up `data/profiles/` and `manifests/` to keep your progress and runtime records.

## How setup uses the game files

- The launcher ships no package unpacker and embeds no unpacking code or keys. It takes an unpacked game folder, or a package file together with an unpacker program the player supplies (`orbis-unpkg.exe` or `ps4-pkg-tool.exe`), which it runs after showing its path and asking once. A package is unpacked into a staging folder, verified, and moved into `data/games/CUSA35521/base` and `update`.
- An unpacked folder is used where it is. The launcher verifies it and records its location in `data/games/CUSA35521/game.json`; it does not copy the game into `data/games/` and never writes inside the game folder.
