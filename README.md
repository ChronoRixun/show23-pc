# show23-pc

MLB The Show 23 on PC, with a local stand-in for the online card mode.

This project runs your own copy of the PlayStation 4 game *MLB The Show 23* (`CUSA35521`) on a Windows PC through a native game engine based on [mlb_pc](https://github.com/victorgbd/mlb_pc). It also includes a small server that runs on your own machine and answers the game where the publisher's servers used to, so that Diamond Dynasty and its single-player modes keep working now those servers are gone.

**Status: 0.2.0.** Download the Windows native portable ZIP from the [launcher repository](https://github.com/ChronoRixun/show23-pc-launcher/releases) and follow its [player guide](https://github.com/ChronoRixun/show23-pc-launcher#for-players). This repository holds the server, the developer tools and the design notes. See [docs/status.md](docs/status.md) for what has been seen working in the game and [docs/roadmap.md](docs/roadmap.md) for what comes next.

## What you will need

- Windows 10 or 11, 64-bit, and a graphics card with Vulkan support and current drivers.
- Your own, legally obtained copy of MLB The Show 23 for PS4 (`CUSA35521`), version 1.00. The optional 1.21 update supplies most special-card pictures.
- If you're setting up from a package file, your own unpacker (`orbis-unpkg.exe` or `ps4-pkg-tool.exe`) and about 60 GB of free disk space for the unpacked copy. An unpacked game folder is used in place.

This repository contains **no game files**: no executables, art, audio, rosters, card data, keys or certificates. Everything the game needs is read from the copy you supply, on your machine. The release download contains no game files and no card art either.

## How it fits together

| Part | Where | Licence |
|---|---|---|
| Launcher: checks your game, sets up the install, prepares game files and starts the engine and server | separate repository, [`show23-pc-launcher`](https://github.com/ChronoRixun/show23-pc-launcher) | GPL-3.0 |
| Local server: plays the part of the game's online service on `127.0.0.1` | [`server/`](server/README.md) | MIT |
| Developer tools: read-only utilities for packages and rosters | [`tools/`](tools/README.md) | MIT |
| Native game engine and preparation scripts | [`mlb_pc` fork](https://github.com/ChronoRixun/mlb_pc/tree/show23), branch `show23` | GPL-2.0-or-later |

More detail is in [docs/architecture.md](docs/architecture.md). Updates, the Stable and Beta channels and mods are described in [docs/launcher.md](docs/launcher.md).

## Set up and play

1. Extract the entire `*-win64-native-portable.zip` into a new folder. Keep `show23-pc.exe` and the `show23-pc` folder together.
2. Run `show23-pc.exe`, choose **Set up**, select your game files and pick a fresh install folder. You can reuse your unpacked game files.
3. Press **Play**. The first launch prepares game files and can take a few minutes. Later launches are quicker. Closing the game also stops the local server.

Older launchers cannot update to 0.2.0 in place. Game saves and Diamond Dynasty progress start fresh, and the game shows its first-launch tutorial again. The 0.1.x releases used the [shadPS4 emulator build](https://github.com/ChronoRixun/show23-pc-emulator/tree/release) and remain available from the launcher Releases page.

**Settings** lets you choose a local player name, a 720p or 1080p window (1080p by default), or fullscreen at your desktop size. Play always runs game version 1.00. The optional 1.21 update is used for card pictures.

Games run at a higher frame rate than on the emulator build, and the distorted triangles seen on players in close-ups are fixed.

## Diamond Dynasty

- **Sign-in** uses the server's fixed account name. You start with a 26-player squad of Common cards built from the game's own roster and 5,000 Stubs.
- **Modes and features:** Play vs. CPU with results, XP and Stubs kept; ten repeatable Daily Missions; Mini Seasons; Conquest (Batter Up 1, World Baseball Classic and Pennant Chase maps); Showdown (Starter, World Baseball Classic, Pennant Chase); Moments; Programs including Team Affinity, World Baseball Classic, Monthly Awards and Pennant Chase; the pack shop paid for with Stubs; exchanges; collections, inventory and team equipment.
- **Authored content:** missions, programs, Moments, Conquest maps and Showdowns are data files in `server/content/CUSA35521/<revision>/`, checked when the server starts; the server refuses to start with a clear message if a file is invalid.
- **Optional special-card import:** the launcher can fetch the card list and missing pictures from the publisher's public website on request.

If an existing progress file cannot be read, the server leaves it intact. Storylines is unavailable on the local server and now shows a message instead of crashing. The Community Market and buying Stubs remain switched off. The server runs on your own PC and supports single-player play only.

## Where your files are

| What | Where inside the install |
|---|---|
| Game saves and settings | `data/profiles/default/engine/` |
| Diamond Dynasty progress and this install's certificates | `data/profiles/default/server/` |
| Optional special-card import | `data/import-cache/` |
| Preparation, engine, server and card-import logs | `data/logs/` |

Back up `data/profiles/` and `manifests/` to keep your progress and runtime records.

## Manual mode (no launcher)

From the freshly extracted portable release, follow `manual/README.txt`. Run `manual\setup.cmd <install> <base> [update]` with unpacked game folders, then `manual\play.cmd <install> [--fullscreen]` to start the native engine and local server. You can select the same install in the launcher later. The Linux command-line wrappers are retired for this release.

## Known limits

- Testing so far covers one PC with an NVIDIA graphics card.
- Shaders compile on first use, so the first few minutes of play can stutter.
- A full CPU game has not yet been played from start to finish in the release build.
- The Captain card feature is unverified.
- Choice packs are not supported.
- Trophies are unsupported in this release.
- In-game videos and the optional special-card import have yet to be retested on the native engine.

The card import is optional and uses the publisher's public website from the player's own PC. Players should be aware of that site's terms before using it.

## Reporting problems

Problems with setup, Play or the card import: [launcher issues](https://github.com/ChronoRixun/show23-pc-launcher/issues). Problems inside Diamond Dynasty (a screen that will not load, a reward that is wrong): [issues here](https://github.com/ChronoRixun/show23-pc/issues), with the newest `server-*.log` from the install's `data/logs/` folder.

## Credits

- [mlb_pc](https://github.com/victorgbd/mlb_pc) and its contributors provided the basis for the native game engine. The Show 23 changes are in the [`show23` branch of the fork](https://github.com/ChronoRixun/mlb_pc/tree/show23).
- [shadPS4](https://github.com/shadps4-emu/shadPS4) and its contributors: the native engine's renderer and shader recompiler are built on shadPS4 (through mlb_pc), and the 0.1.x releases ran the game on a shadPS4 build.
- The launcher is adapted from [Ultimate Legends](https://github.com/ChronoRixun/ultimate-legends), which descends from the [CB Servers Launcher](https://github.com/CBServers/cb-launcher) and the [XLabs Launcher](https://github.com/XLabsProject/launcher).
- [showdd.io](https://www.showdd.io/), a community card database, was a reference during research for which cards past Chase Packs and Diamond Duos featured. The server does not use it.
- The package carries third-party notices, including for its LGPL-2.1-or-later build of FFmpeg. The portable version folder contains `LICENSES/engine/SOURCE.txt` for the corresponding engine source. Python and Pillow carry their notices with the server files. See the [bundled software list](https://github.com/ChronoRixun/show23-pc-launcher#bundled-software).

## Legal

This is an unofficial fan project. It is not affiliated with, endorsed by or connected to Sony Interactive Entertainment, San Diego Studio, Major League Baseball, the MLB Players Association or the shadPS4 project. *MLB The Show* and related names are trademarks of their owners and are used here only to say what the software works with.

You must own the game. Do not ask for, and do not post, game files, keys or accounts in this repository's issues or discussions. See [docs/publishing-rules.md](docs/publishing-rules.md) for what is never accepted into the source tree.

The code in this repository is released under the [MIT licence](LICENSE). The launcher is GPL-3.0 and the native game engine is GPL-2.0-or-later.
