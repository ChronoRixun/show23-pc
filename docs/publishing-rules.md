# What never goes into this repository

These rules apply to every commit, issue, discussion and release download.

## Never included

- **Game files of any kind**: packages, executables, converted or decrypted binaries, art, audio, video, shaders, rosters, text extracted from the game.
- **Keys and certificates**: the game's own, the console's, or any install's generated ones. The local server creates a fresh certificate authority for each install under `data/profiles/default/server/`.
- **Card catalogue data and pictures** from the game's website, and anything derived from them.
- **Anything from a person's account**: inventories, saves, state files, logs that carry account identifiers.
- **Decompiled or disassembled game code.** Notes may give an address and say what a function reads or writes; they do not reproduce the code.

## How it is enforced

- `.gitignore` blocks the file types and folders above.
- Source files are copied in from the development tree by a script that carries an explicit list of files, then scans the result for personal details, workstation paths, key and certificate text, and oversized or binary files. A commit is only made from a clean scan.
- Build release downloads outside working installs, using a clean launcher build, a staged copy of the server and the [native engine package](https://github.com/ChronoRixun/mlb_pc/tree/show23). The portable ZIP carries the third-party notices and the corresponding engine source link. See the [launcher packaging instructions](https://github.com/ChronoRixun/show23-pc-launcher#package-the-playable-release).

## Reporting a problem

If you find something in the repository that should not be there, open an issue that says where it is without quoting it.
