# How the project is put together

There are three parts in three repositories: the local server and its tools here, the emulator in its own, and the launcher in its own.

```text
 your game package(s)                     this repository                 show23-pc-emulator
 ┌──────────────────┐   checks, unpacks   ┌──────────────┐    starts      ┌────────────────┐
 │ base game  1.00  │ ──────────────────▶ │   builder /  │ ─────────────▶ │  shadPS4 build │
 │ update     1.21  │                     │   launcher   │                │  for this game │
 └──────────────────┘                     └──────┬───────┘                └───────┬────────┘
                                                 │ starts                         │ the game's online
                                                 ▼                                │ calls, on 127.0.0.1
                                          ┌──────────────┐ ◀──────────────────────┘
                                          │ local server │
                                          └──────────────┘
```

## Launcher and builder (`show23-pc-launcher`, separate repository)

Takes the player's own game package or unpacked folder, verifies it is the supported title and version, unpacks it into an install folder the player chooses, writes emulator settings, and from then on starts the emulator and the local server together and stops them together. It accepts the base game (version 1.00) and, optionally, the 1.21 update package.

It also keeps the parts up to date through Stable and Beta channels and manages mods; see [launcher.md](launcher.md). The install layout the builder creates is described in [builder/README.md](../builder/README.md). All game data, saves and per-player state live under the install's `data/` folder and never in a source tree.

## Local server (`server/`)

A single Python program. The game signs in to it instead of the publisher's servers and sends it the same requests it would have sent them; the server answers from data on the player's machine.

- It listens only on `127.0.0.1`, and the emulator build refuses to let the game reach any other address while local mode is on.
- It creates its own certificate authority and server certificate on first run, per install. No key or certificate from the game is used or stored.
- The player's cards are built from the roster files of the player's own game copy. An optional, player-started import can add the public card catalogue and pictures from the game's website while that site is still up.
- Progress (squad, Stubs, XP, packs, mode progress) is kept in one state file under the install's `data/` folder.

The wire format and the request names the server handles are summarised in [protocol.md](protocol.md).

## Emulator (`show23-pc-emulator`, separate repository)

A fork of shadPS4, kept under shadPS4's own licence. It carries the rendering and memory changes this game needs, and the small set of changes that let the game sign in to the local server: a local identity, routing of the game's service hosts to `127.0.0.1`, trust for the per-install certificate authority, and the rule that nothing but loopback is reachable in local mode. Each of these is tied to the exact game executable it was written for.

## Developer tools (`tools/`)

Read-only helpers used while working on the project: a package header reader, a decoder for the game's roster files, and the name hash the game's reply parser uses. They read from the developer's own game copy and write nothing into it.
