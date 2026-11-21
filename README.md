# show23-pc

MLB The Show 23 on PC, with a local stand-in for the online card mode.

This project runs your own copy of the PlayStation 4 game *MLB The Show 23* (`CUSA35521`) on a Windows PC through a game-specific build of the [shadPS4](https://github.com/shadps4-emu/shadPS4) emulator. It also includes a small server that runs on your own machine and answers the game where the publisher's servers used to, so that Diamond Dynasty and its single-player modes keep working now those servers are gone.

**Status: 0.1.0, the first release.** Players: download the release from the [launcher repository](https://github.com/ChronoRixun/show23-pc-launcher/releases) and follow its [README](https://github.com/ChronoRixun/show23-pc-launcher#for-players). This repository holds the server, the developer tools and the design notes. See [docs/status.md](docs/status.md) for what has been seen working in the game and [docs/roadmap.md](docs/roadmap.md) for what comes next.

## What you will need

- Windows 10 or 11 x64 and a graphics card with Vulkan support.
- Your own, legally obtained copy of MLB The Show 23 for PS4, version 1.00, as an unpacked folder, or as a package file plus an unpacker program of your own. The 1.21 update is optional; it supplies most special-card pictures.

This repository contains **no game files**: no executables, art, audio, rosters, card data, keys or certificates. Everything the game needs is read from the copy you supply, on your machine. The release download contains no game files and no card art either.

## How it fits together

| Part | Where | Licence |
|---|---|---|
| Launcher: checks your game, sets up the install, imports special cards on request, keeps the parts up to date and starts everything | separate repository, [`show23-pc-launcher`](https://github.com/ChronoRixun/show23-pc-launcher) | GPL-3.0 |
| Local server: plays the part of the game's online service on `127.0.0.1` | [`server/`](server/README.md) | MIT |
| Developer tools: read-only helpers for packages and rosters | [`tools/`](tools/README.md) | MIT |
| Emulator: shadPS4 with the changes this game needs | separate repository, [`show23-pc-emulator`](https://github.com/ChronoRixun/show23-pc-emulator), branch `release` | GPL-2.0-or-later (as upstream) |

More detail is in [docs/architecture.md](docs/architecture.md). Updates, the Stable and Beta channels and mods are described in [docs/launcher.md](docs/launcher.md).

## What works in 0.1.0

- **Setup** from an unpacked 1.00 base folder (and optionally the 1.21 update), or from your own package files through an unpacker you supply.
- **Play** starts the local server and the emulator together and stops them together. The game always runs as version 1.00.
- **Diamond Dynasty** signs in to the local server with a name you choose, a 26-player squad of Common cards built from the game's own roster, and 5,000 starting Stubs.
- **Modes and features:** Play vs. CPU with results, XP and Stubs kept; Mini Seasons; Conquest; Showdown; Programs including Team Affinity; the pack shop paid for with Stubs; exchanges; collections, inventory and team equipment.
- **Optional special-card import:** on request, the player's own PC fetches the card list and the missing pictures from the publisher's public website (about 35 MB and 5 minutes). It adds about 1,775 special cards, shown with their designed art (mostly from the player's own 1.21 update files), and 40 event packs (Chase Packs, Diamond Duos and the Lou Gehrig Day pack).

The Community Market and buying Stubs are deliberately switched off. There is no play between people: the server only ever talks to the game on the same machine.

## Known limits

- Frame rate: small stutters, and a played game does not hold a steady 60 frames a second.
- A full CPU game has not yet been played from start to finish in the release build.
- The Captain card feature is unverified.
- About 211 special cards are left out of the import because they could not be matched to a player in the game's roster.
- Choice packs are not supported.
- The launcher's self-update has not been exercised against a real release yet.
- Only game version 1.00 is played; Windows only.

The card import is optional and uses the publisher's public website from the player's own PC. Players should be aware of that site's terms before using it.

## Reporting problems

Problems with setup, Play or the card import: [launcher issues](https://github.com/ChronoRixun/show23-pc-launcher/issues). Problems inside Diamond Dynasty (a screen that will not load, a reward that is wrong): [issues here](https://github.com/ChronoRixun/show23-pc/issues), with the newest `server-*.log` from the install's `data/logs/` folder.

## Credits

- [shadPS4](https://github.com/shadps4-emu/shadPS4) and its contributors: the PS4 emulator the game runs on, including its renderer and shader recompiler. Our emulator is a fork of it under GPL-2.0-or-later.
- [mlb_pc](https://github.com/victorgbd/mlb_pc), the PC port of MLB The Show 24: an inspiration and a reference. The fix notes in its `MLB_PORT.md` on this engine's crowd, nets, faces and GPU counters informed several of our engine fixes.
- The launcher is adapted from [Ultimate Legends](https://github.com/ChronoRixun/ultimate-legends), a fork of the [CB Servers Launcher](https://github.com/CBServers/cb-launcher) by CB Servers, whose foundation is the [XLabs Launcher](https://github.com/XLabsProject/launcher) by momo5502 and the XLabs team.
- [showdd.io](https://www.showdd.io/), a community card database, was a reference during research for which cards past Chase Packs and Diamond Duos featured. The server does not use it.
- The release bundles [Python](https://www.python.org/) 3.14.7 (PSF License 2.0) to run the server and [Pillow](https://python-pillow.org/) 13.0.0 (MIT-CMU) to convert imported card pictures, along with the launcher's own libraries (Chromium Embedded Framework and Chromium, libcurl, RapidJSON, miniz, LibTomCrypt, LibTomMath) and the Oswald font (SIL Open Font License 1.1). Each licence text is in the `LICENSES` folder of the release zip; the full list is in the [launcher README](https://github.com/ChronoRixun/show23-pc-launcher#bundled-software).

## Legal

This is an unofficial fan project. It is not affiliated with, endorsed by or connected to Sony Interactive Entertainment, San Diego Studio, Major League Baseball, the MLB Players Association or the shadPS4 project. *MLB The Show* and related names are trademarks of their owners and are used here only to say what the software works with.

You must own the game. Do not ask for, and do not post, game files, keys or accounts in this repository's issues or discussions. See [docs/publishing-rules.md](docs/publishing-rules.md) for what is never accepted into the source tree.

The code in this repository is released under the [MIT licence](LICENSE). The emulator and the launcher are separate works under their own licences.
