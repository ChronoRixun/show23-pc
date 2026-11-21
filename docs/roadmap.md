# Roadmap to a first release

The first public release waits until the card mode is part of it. Frame rate in a played game (about 35 frames a second today) is being worked on alongside. The steps, in the order they depend on each other:

1. **Cards from the player's own game copy.** Build the full Live Series card list, ratings, player looks and pitch data from the roster files in the game package, with no website data shipped. The roster decoder in `tools/` already reads every player record; what remains is turning that into the collection the server serves.
2. **A curated emulator branch.** Done: the emulator repository has a `release` branch with only the fixes the game needs, the measured performance changes and the local sign-in support, the last tied to the exact game executable. It has been booted and played briefly; a full play-through is still owed. The development history stays on its own branch.
3. **The launcher and builder.** The launcher is adapted from an existing one, with update channels and a mods screen ([launcher.md](launcher.md)). Done: the window, and setup for an unpacked game folder, which is checked, verified and used where it is. A package file is identified but not unpacked: the launcher ships no unpacker. Still to do: installing the emulator build and the server, writing the emulator settings, and starting and stopping the two together.
4. **Server paths and data.** Done: started with `--install`, the server takes the game location from the launcher's record and keeps all per-player state under the install's `data/` folder. It has been started that way; no game has signed in to it in that mode yet.
5. **Version 1.21 with the local server.** The update's executable already runs offline; the sign-in support has to be ported to it.
6. **Packaging.** One Windows download with the launcher, the emulator build, notices and a checksum list; the matching emulator source published alongside it.
7. **Content packs.** The server's Showdown, Conquest, program and pack data moves into the pack format, so the built-in content is the first pack and others can be added.
8. **Optional catalogue import.** A button the player presses to fetch the public card catalogue and pictures from the game's website into their own install, for as long as that site exists.

## Still open in the card mode

- Showdown moments that can be retried, and the real ten-run final.
- Conquest maps beyond the first tutorial map.
- Other Programs, playoff games in Mini Seasons, equipment for the created ballplayer, the custom team logo and uniform.
