# Roadmap to a first release

The first public release waits until the card mode is part of it. The steps, in the order they depend on each other:

1. **Cards from the player's own game copy.** Build the full Live Series card list, ratings, player looks and pitch data from the roster files in the game package, with no website data shipped. The roster decoder in `tools/` already reads every player record; what remains is turning that into the collection the server serves.
2. **A curated emulator branch.** The emulator repository currently holds the whole working history, including diagnostics. Before release the changes are sorted into a clean branch: the fixes the game needs, the local sign-in support tied to the exact game executable, and nothing else.
3. **The builder.** Package intake for the base game and the optional 1.21 update, checks on title, version and executable, unpacking into a staging folder, generated emulator settings, and one launcher that starts and stops the emulator and the server together. An unpacked game folder is accepted as well as a package file.
4. **Server paths and data.** The server still expects the folder layout of the development tree. It moves to the install layout in `builder/README.md`, with all per-player state under `data/`.
5. **Version 1.21 with the local server.** The update's executable already runs offline; the sign-in support has to be ported to it.
6. **Packaging.** One Windows download with the launcher, the emulator build, notices and a checksum list; the matching emulator source published alongside it.
7. **Optional catalogue import.** A button the player presses to fetch the public card catalogue and pictures from the game's website into their own install, for as long as that site exists.

## Still open in the card mode

- Team Affinity list screen does not open in the game.
- Showdown moments that can be retried, and the real ten-run final.
- Conquest maps beyond the first tutorial map.
- Other Programs, playoff games in Mini Seasons, equipment for the created ballplayer, the custom team logo and uniform.
