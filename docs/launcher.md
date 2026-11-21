# Launcher, updates and mods

**Status: design.** Nothing described here is built yet. It records the decisions so that the server, the emulator fork and the game profiles are shaped to fit.

## What the launcher is built from

The launcher will be based on the launcher of [Ultimate Legends](https://github.com/ChronoRixun/ultimate-legends), by the same author. That launcher is a fork of the [CB Servers Launcher](https://github.com/CBServers/cb-launcher), whose foundation is the XLabs Launcher originally developed by [momo5502](https://github.com/momo5502) and the XLabs team. All of them are licensed under the GPL-3.0.

Two consequences:

- **The launcher is GPL-3.0, not MIT**, and lives in its own repository, `show23-pc-launcher`, with the credits above kept. This repository (server, tools, documents) stays MIT; the emulator fork stays under shadPS4's licence. The three are separate programs.
- The work is an adaptation, not a rewrite. What already exists and is reused: the self-updating portable build, downloads checked by size and SHA-256 against a release's checksum file, the Stable and Beta channels, Verify Files, the mods screen, portable and installed modes, the offline switch, and a window that works with keyboard, mouse and controller. What is removed: the game entries of that project and their artwork. What is added: one game entry whose setup is the builder described in [`builder/README.md`](../builder/README.md).

## The parts and how each is versioned

A working install is made of four parts that change at different speeds. Each is published as a release with a checksum file and is fetched by the launcher.

| Part | Published from | Changes when | Typical pace |
|---|---|---|---|
| Launcher | `show23-pc-launcher` | The launcher or builder changes | Rare |
| Emulator build | `show23-pc-emulator` (with its matching source) | A rendering, performance or compatibility fix lands | Occasional |
| Local server | this repository | Card-mode features and fixes | Frequent |
| Game profile | this repository | Support for a game version is added or corrected | Rare |

A **game profile** is the small set of facts tied to one exact game executable: its checksum, the fixed places in it that the emulator's local-mode support relies on, and the emulator settings the game needs. Version 1.00 and version 1.21 of the game are two profiles.

Every release carries a short manifest: its version, its channel, the checksum of each file, the oldest launcher that can install it, and which versions of the other parts it works with (for example: this server needs emulator build N or newer and profile format 2). The launcher never installs a combination the manifests do not allow.

## Update channels

**Settings > Updates > Update channel**, as in Ultimate Legends:

- **Stable** (the default) gets full releases only.
- **Beta** also gets pre-releases as soon as they are published.
- The channel applies to all four parts. The screen lists the exact version of each part that is installed, with a badge on a pre-release.
- Nothing is downgraded without asking. Switching from Beta back to Stable offers to return each part to the latest full release, or to keep what is installed until Stable catches up.
- Saves, the player's game copy, server state, mods and settings are never touched by an update: program files live under `runtime/`, everything that belongs to the player under `data/`.
- An update that fails or is interrupted leaves the previous version in place.

Because the local server changes far more often than the emulator, Beta mostly means "newer server": new modes and fixes reach testers quickly while Stable stays on a combination that has been played through.

## The builder inside the launcher

Setting the game up is the launcher's first-run flow for its one game entry: choose the base game package or an unpacked folder, optionally the 1.21 update package, check them, unpack into staging, verify, adopt. The checks and the install layout are in [`builder/README.md`](../builder/README.md). **Verify Files** re-runs the checks on an existing install and repairs the program parts, never the player's data.

## Mods

Three kinds, in the order they will be supported.

### 1. Content packs for the local server

The server's card-mode content is plain data: Showdown tracks and their moments, Conquest maps and goals, Team Affinity tasks and reward tracks, pack definitions, exchange values. A content pack adds to or replaces that data.

- A pack is a folder or a zip with a `pack.json` (an id, a name, a version, an author, the server version it needs) and data files grouped by kind (`showdowns/`, `conquest/`, `programs/`, `packs/`).
- **Data only.** A pack cannot contain code or scripts. The server checks every file against a schema with ranges and size limits when it starts; a pack that fails is skipped with a message and never stops the server.
- Ids are namespaced by pack id, so two packs cannot collide, and a player's progress in a pack's content is stored under that id. Disabling a pack hides its content and keeps the progress.
- The launcher's Mods screen lists the packs in the install's `data/mods/` folder with an on/off switch each. Changes take effect at the next start of the game.

No game file is involved, so a pack can be shared freely by whoever made it.

### 2. Rosters and card sets

A roster or card set is a content pack kind of its own: a list of cards (player, ratings, positions, series, rarity) that the server offers alongside the ones it builds from the player's game copy. Pictures for such cards must come with the pack or already be in the player's game.

### 3. Game file overlays

Replacement uniforms, stadium textures or audio work by layering a folder over the game's files, the same way the 1.21 update is layered over the base game. This depends on work in the emulator fork and comes last. The launcher will manage the folder and the load order; it will never download or host such files.

## What the launcher contacts

- The release pages of the three repositories, to check for and download updates. `-offline` turns this off.
- Nothing else by default. The optional import of the public card catalogue from the game's website is a button the player presses.

## Open points

- Whether releases are signed, and by which key.
- Whether a fresh install downloads the emulator build and the server as separate parts or ships one combined archive for the first run.
- The size of the launcher's embedded browser component and whether it is fetched at first run.
- The exact schema of content packs, to be written when the first pack is made from the built-in content.
