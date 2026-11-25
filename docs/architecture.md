# How the project is put together

There are three main parts. This repository holds the local server and its tools. The launcher and the native game engine live in their own repositories.

```text
 your game files                         show23-pc-launcher                 mlb_pc (show23)
 ┌──────────────────┐  checks, prepares   ┌──────────────┐    starts      ┌────────────────┐
 │ base game  1.00  │ ──────────────────▶ │   builder /  │ ─────────────▶ │  native game   │
 │ update     1.21  │                     │   launcher   │                │     engine     │
 └──────────────────┘                     └──────┬───────┘                └───────┬────────┘
                                                 │ starts                         │ the game's online
                                                 ▼                                │ calls, on 127.0.0.1
                                          ┌──────────────┐ ◀──────────────────────┘
                                          │ local server │  (this repository)
                                          └──────────────┘
```

## Launcher and builder (`show23-pc-launcher`, separate repository)

Setup verifies the player's PS4 game files (`CUSA35521`, version 1.00) and stages the bundled engine and server in the chosen install folder. It uses unpacked folders in place and extracts packages through an unpacker the player supplies. The optional 1.21 update is used for card pictures.

On Play, the launcher prepares and verifies a boot and content cache from the base game under `data/engine-cache/CUSA35521/`. It starts the local server, waits for its loopback listeners, then starts the engine. The server stops when the session ends. See [launcher.md](launcher.md) for release channels and mods and [builder/README.md](../builder/README.md) for the install layout.

## Local server (`server/`)

A single Python program. The game signs in to it instead of the publisher's servers and sends it the same requests it would have sent them; the server answers from data on the player's machine.

- It listens only on `127.0.0.1`. During Play, the game's service requests go to the local server.
- It creates its own certificate authority and server certificate on first run, per install. No key or certificate from the game is used or stored.
- The player's cards are built from the roster files of the player's own game copy. An optional, player-started import can add the public card catalogue and pictures from the game's website while that site is still up.
- Progress (squad, Stubs, XP, packs, mode progress) and the install's certificates are kept under `data/profiles/default/server/`. If the progress file exists but cannot be read, the server leaves it intact.

The wire format and the request names the server handles are summarised in [protocol.md](protocol.md).

## Native game engine (`mlb_pc`, branch `show23`)

The [engine fork](https://github.com/ChronoRixun/mlb_pc/tree/show23) adapts [mlb_pc](https://github.com/victorgbd/mlb_pc) for Show 23 and is distributed under GPL-2.0-or-later. The prepared game files stay in the install. The engine keeps saves and settings under `data/profiles/default/engine/`. Session logs go to `data/logs/`.

The 0.1.x releases used a separate [build of the shadPS4 emulator](https://github.com/ChronoRixun/show23-pc-emulator/tree/release), with local sign-in support tied to the 1.00 game executable.

## Developer tools (`tools/`)

Read-only utilities include a package header reader, a decoder for the game's roster files, and the name hash the game's reply parser uses. They read from the developer's own game copy.
