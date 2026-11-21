# show23-pc

MLB The Show 23 on PC, with a local stand-in for the online card mode.

This project runs your own copy of the PlayStation 4 game *MLB The Show 23* on a Windows PC through a game-specific build of the [shadPS4](https://github.com/shadps4-emu/shadPS4) emulator. It also includes a small server that runs on your own machine and answers the game where the publisher's servers used to, so that Diamond Dynasty and its single-player modes keep working after those servers are gone.

**Status: pre-release.** The repository is being set up. Nothing here is packaged for players yet; see [docs/status.md](docs/status.md) for what works today and [docs/roadmap.md](docs/roadmap.md) for what is left before a first release.

## What you will need

- Your own, legally obtained copy of MLB The Show 23 for PS4 (title `CUSA35521`), as a package file or an unpacked folder. The 1.21 update package is optional.
- A Windows PC with a recent graphics card and about 60 GB of free disk space.

This repository contains **no game files**: no executables, art, audio, rosters, card data, keys or certificates. Everything the game needs is read from the copy you supply, on your machine.

## How it fits together

| Part | Where | Licence |
|---|---|---|
| Launcher: checks your game, sets up the install, keeps the parts up to date and starts everything | separate repository, [`show23-pc-launcher`](https://github.com/ChronoRixun/show23-pc-launcher) | GPL-3.0 |
| Local server: plays the part of the game's online service on `127.0.0.1` | `server/` | MIT |
| Developer tools: read-only helpers for packages and rosters | `tools/` | MIT |
| Emulator: shadPS4 with the changes this game needs | separate repository, [`show23-pc-emulator`](https://github.com/ChronoRixun/show23-pc-emulator), branch `release` | GPL-2.0-or-later (as upstream) |

More detail is in [docs/architecture.md](docs/architecture.md). Updates, the Stable and Beta channels and mods are described in [docs/launcher.md](docs/launcher.md).

## What works in the card mode today

Playing against the CPU with your own squad, Mini Seasons, Conquest, Showdown, a pack shop paid for with Stubs earned in games, XP and the reward path, Team Affinity, collections, inventory, team equipment and player exchanges. Each of these is at a different level of polish; the table in [docs/status.md](docs/status.md) says exactly which parts have been seen working in the game and which are still being built.

There is no play between people. The server only ever talks to the game on the same machine.

## Credits

This project builds on the work of others:

- [shadPS4](https://github.com/shadps4-emu/shadPS4): the emulator the game runs on, including its renderer and shader recompiler.
- [mlb_pc](https://github.com/victorgbd/mlb_pc), the PC port of MLB The Show 24: its notes on this engine's crowd, nets, faces and GPU counters helped find several of the fixes this game needed.
- The launcher is adapted from [Ultimate Legends](https://github.com/ChronoRixun/ultimate-legends), a fork of the [CB Servers Launcher](https://github.com/CBServers/cb-launcher), whose foundation is the [XLabs Launcher](https://github.com/XLabsProject/launcher) by momo5502 and the XLabs team.
- The libraries each part bundles are listed with their licences in that part's repository and in the release package.

## Legal

This is an unofficial fan project. It is not affiliated with, endorsed by or connected to Sony Interactive Entertainment, San Diego Studio, Major League Baseball, the MLB Players Association or the shadPS4 project. *MLB The Show* and related names are trademarks of their owners and are used here only to say what the software works with.

You must own the game. Do not ask for, and do not post, game files, keys or accounts in this repository's issues or discussions. See [docs/publishing-rules.md](docs/publishing-rules.md) for what is never accepted into the source tree.

The code in this repository is released under the [MIT licence](LICENSE). The emulator and the launcher are separate works under their own licences.
