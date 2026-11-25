# Roadmap

## Done in 0.1.0

1. **Cards from the player's own game copy.** The server builds the card pool (2,790 Live cards from game version 1.00), the player records, looks, pitch data and a picture index from the game folder, and gives a new player a 26-man starter squad and 5,000 Stubs.
2. **A curated emulator branch.** The [emulator repository's `release` branch](https://github.com/ChronoRixun/show23-pc-emulator/tree/release) carried the game fixes and the local sign-in support for 0.1.x.
3. **The launcher.** Setup from an unpacked folder or, through an unpacker the player supplies, from a package file; Play starts and stops the server and the emulator together; a player name; update channels and a mods screen ([launcher.md](launcher.md)).
4. **Server paths and data.** Started with `--install`, the server takes the game location from the launcher's record and keeps all per-player state under the install's `data/` folder. Its certificates are made in pure Python, so the release needs no other program.
5. **Packaging.** One Windows download with the launcher, the emulator build, the server, an embedded Python, the licence notices and a checksum list; the matching emulator source is named in it.
6. **Optional special-card import.** A button the player presses to fetch the public card catalogue and missing pictures from the publisher's website into their own install, with 40 event packs (Chase Packs, Diamond Duos).

## Done in 0.2.0

- **Native game engine.** The [Show 23 branch of mlb_pc](https://github.com/ChronoRixun/mlb_pc/tree/show23) replaces the emulator. It raises in-game frame rates and fixes the distorted triangles on players in close-ups.
- **Windows portable setup and play.** A fresh install stages the bundled engine and server. Both the launcher and Windows command-line Play prepare and start the engine.
- **Progress protection.** The server retries a failed read and leaves existing progress intact if it still cannot read the file.
- **Storylines message.** Opening the unavailable mode now shows a message instead of crashing.

## Next

- Test on more PC and graphics card configurations and check stutters during play.
- Play a full CPU game start to finish in the native release build, check Captain cards and retest in-game videos and special-card import.
- **Version 1.21.** The native release plays 1.00 and reads the optional 1.21 update for card pictures. Playing 1.21 remains open.
- **Content packs.** The launcher handles local pack folders and their enabled state. The server still needs to load the pack format for Showdown, Conquest, program and pack data.
- Check the native launcher's update path against a later native release.

## Still open in the card mode

- Choice packs, and the special cards the import cannot yet match to a roster player.
- Showdown moments that can be retried, and the real ten-run final.
- Conquest maps beyond the first tutorial map.
- Other Programs, playoff games in Mini Seasons, equipment for the created ballplayer, the custom team logo and uniform.
