# Launcher, updates and mods

The Windows launcher in 0.2.0 sets up the game and starts the native engine and local server. See the [launcher player guide](https://github.com/ChronoRixun/show23-pc-launcher#for-players) for the portable download and setup instructions.

## What the launcher is built from

The launcher is adapted from [Ultimate Legends](https://github.com/ChronoRixun/ultimate-legends), which descends from the [CB Servers Launcher](https://github.com/CBServers/cb-launcher) and the [XLabs Launcher](https://github.com/XLabsProject/launcher).

The launcher lives in `show23-pc-launcher` under GPL-3.0. This repository's code stays MIT, and the [native engine](https://github.com/ChronoRixun/mlb_pc/tree/show23) is GPL-2.0-or-later.

## The parts and how each is versioned

A working install has four versioned parts. The native portable bundle supplies the runtime that Setup stages into the install.

| Part | Source | Changes when |
|---|---|---|
| Launcher | `show23-pc-launcher` | The launcher or builder changes |
| Game engine | `mlb_pc`, branch `show23` | A rendering, performance or compatibility fix lands |
| Local server | this repository | Card-mode features and fixes |
| Game profile | this repository | The checks for the supported game files change |

A **game profile** describes the supported game files, including executable checksums. The game runs as version 1.00. Setup accepts 1.21 as an optional source of card pictures.

The manifests record versions, channels, file checksums and compatibility rules. The engine still uses `emulator` as its manifest key and runtime directory name. See the [release manifest format](https://github.com/ChronoRixun/show23-pc-launcher/blob/main/docs/release-manifest.md).

## Update channels

In **Settings > Updates**, Stable selects final native releases and Beta also includes native pre-releases. The page lists the launcher, game engine, server and profile.

0.2.0 requires a fresh ZIP extraction and install folder. Older launchers cannot update to it in place. Later portable launcher updates can replace the launcher version while keeping the active runtime. Download-based installation of separate runtime parts is pending.

## The builder inside the launcher

Choose the 1.00 base game and optionally the 1.21 update. Setup verifies unpacked folders in place or uses a player-supplied unpacker to extract packages into staging. See [`builder/README.md`](../builder/README.md) for the checks and install layout.

**Verify Files** checks the game folders and installed runtime while Play is stopped. **Repair runtime** can restore the active runtime from a matching verified bundle.

## Mods

The Mods screen manages local packs under `data/mods/`. It can import a folder or ZIP with a `pack.json`, record which packs are enabled and save their order. Loading packs into the server or game is future work.

### 1. Content packs for the local server

The plan is to let data-only packs supply Showdown tracks, Conquest maps, Programs and pack definitions. The server still needs a schema, validation and loading support. The proposed format groups data by kind and uses each pack's ID to keep its content and player progress separate.

### 2. Rosters and card sets

Proposed roster and card packs would add cards alongside the ones the server builds from the player's game copy.

### 3. Game file overlays

Layering replacement uniforms, stadium textures or audio over the game's files would require engine support.

## What the launcher contacts

- GitHub release listings for the launcher and this repository, and release assets when an update is approved.
- The publisher's public website when the player chooses **Import special cards**.

Start with `-offline`, or enable **Offline mode** in Settings and restart, to disable release checks and card import. See the [launcher privacy notes](https://github.com/ChronoRixun/show23-pc-launcher#privacy).

## Open points

- Whether releases are signed, and by which key.
- The exact schema of content packs, to be written when the first pack is made from the built-in content.
