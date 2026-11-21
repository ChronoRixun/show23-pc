# Builder and launcher

The launcher lives in its own repository. This file records the design so that the pieces written elsewhere fit it. Where the launcher has since settled a point differently, the note at the end says so.

The builder will be the first-run flow of the launcher, which lives in its own repository and is based on an existing launcher; see [`docs/launcher.md`](../docs/launcher.md) for that, for update channels and for mods. This page covers what the builder does with the player's game.

## What the player does

1. Unzip the release, start the launcher, choose an install folder.
2. Point it at their base game: a package file or an already unpacked folder. Optionally add the 1.21 update package.
3. The launcher shows the title, version and the disk space needed, checks the input, unpacks into a staging folder, verifies the result and only then adopts it. A cancelled or failed import can simply be run again.
4. Press Play. The launcher creates the certificate authority for this install on first use, starts the local server, waits until it answers, starts the emulator, and stops both when the game exits.

## Checks before anything is unpacked or started

- The package or folder is the supported title (`CUSA35521`), base game for the base input, update for the update input.
- The version is one this release supports (1.00; 1.21 when its support is finished).
- The game executable matches a known checksum. Features that depend on fixed places in the executable are only enabled for a matching one.
- There is enough free space, and no file would be written outside the staging folder.

## Install layout

```text
<install>/
  Launcher.exe
  runtime/<release>/emulator/        emulator build for this release
  runtime/<release>/server/          the local server
  runtime/<release>/tools/
  licenses/  manifests/
  data/games/CUSA35521/base/         the player's game, unpacked
  data/games/CUSA35521/update/       the 1.21 update, if supplied
  data/profiles/default/user/        emulator saves, settings, input, caches
  data/profiles/default/server/      server state, this install's certificates
  data/import-cache/                 optional website import
  data/logs/
```

`runtime/` is replaced by an update; `data/` belongs to the player and is never touched by one.

## Open points

- Whether a release should bundle a package unpacker (0.1.0 does not).
- Whether the launcher is a small windowed program or a console program for the first release.

## Settled since

- The launcher ships no package unpacker and embeds no unpacking code or keys. It takes an unpacked game folder, or a package file together with an unpacker program the player supplies (`orbis-unpkg.exe` or `ps4-pkg-tool.exe`), which it runs after showing its path and asking once. A package is unpacked into a staging folder, verified, and moved into `data/games/CUSA35521/base` and `update`.
- An unpacked folder is used where it is. The launcher verifies it and records its location in `data/games/CUSA35521/game.json`; it does not copy the game into `data/games/` and never writes inside the game folder.
- The launcher is a windowed program.
