# Roadmap

## Done in 0.1.0

1. **Cards from the player's own game copy.** The server builds the card pool (2,790 Live cards from game version 1.00), the player records, looks, pitch data and a picture index from the game folder, and gives a new player a 26-man starter squad and 5,000 Stubs.
2. **A curated emulator branch.** The emulator repository's `release` branch carries only the fixes the game needs, the measured performance changes and the local sign-in support, the last tied to the exact game executable.
3. **The launcher.** Setup from an unpacked folder or, through an unpacker the player supplies, from a package file; Play starts and stops the server and the emulator together; a player name; update channels and a mods screen ([launcher.md](launcher.md)).
4. **Server paths and data.** Started with `--install`, the server takes the game location from the launcher's record and keeps all per-player state under the install's `data/` folder. Its certificates are made in pure Python, so the release needs no other program.
5. **Packaging.** One Windows download with the launcher, the emulator build, the server, an embedded Python, the licence notices and a checksum list; the matching emulator source is named in it.
6. **Optional special-card import.** A button the player presses to fetch the public card catalogue and missing pictures from the publisher's website into their own install, with 40 event packs (Chase Packs, Diamond Duos).

## Next

- **Frame rate** in a played game (about 35 frames a second today, with small stutters).
- A full CPU game played start to finish in the release build, and the Captain card feature checked.
- **Version 1.21 with the local server.** The update's executable already runs offline; the sign-in support has to be ported to it.
- **Content packs.** The server's Showdown, Conquest, program and pack data moves into the pack format, so the built-in content is the first pack and others can be added.
- The launcher's self-update tried against a real release.

## Still open in the card mode

- Choice packs, and the special cards the import cannot yet match to a roster player.
- Showdown moments that can be retried, and the real ten-run final.
- Conquest maps beyond the first tutorial map.
- Other Programs, playoff games in Mini Seasons, equipment for the created ballplayer, the custom team logo and uniform.
