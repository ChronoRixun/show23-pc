# Local server

`olserver.py` is the whole server: one Python file using the standard library. Pillow is used if it is installed, and `gen-certs` calls an OpenSSL program.

```text
python olserver.py gen-certs     make this install's certificate authority and server certificate
python olserver.py selftest      codec and TLS round trip against itself
python olserver.py run           listen on 127.0.0.1:10000
```

## Current limitation

The file is the working copy from the development tree and still expects that tree's folder layout: it looks for its tables, reply files, state and the unpacked game relative to a project root two levels above itself. It does not run from this folder as laid out here. Moving it to the install layout in [`builder/README.md`](../builder/README.md) is item 4 of the [roadmap](../docs/roadmap.md).

## What is here

| Path | Contents |
|---|---|
| `olserver.py` | Transport, message codec, and every request handler |
| `replies/` | Fixed answers for a handful of requests, as XML written for this project |
| `tables/ol-messages.csv` | Names and type ids of the game's message kinds |

## How the code is organised

Reading the file top to bottom: the codec; sign-in and session; the state file; then one section per feature (profile and XP, programs and Team Affinity, squad and cards, inventory and equipment, collections, shop and packs, Mini Seasons, Showdown, Conquest, exchanges), and last the request router and the network loop.

Comments in the handlers cite the place in the game executable where a value is read (for example `0xf0b080`). Those addresses are for version 1.00 of the game.

## Rules the handlers follow

- An unknown request gets a valid, empty answer, never an error.
- A change to the player's state and the reward that goes with it are saved in one write.
- A request that arrives twice has its effect once.
- Nothing that is the only way to earn something is one-time or capped.
