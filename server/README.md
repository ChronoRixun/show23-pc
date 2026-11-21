# Local server

`olserver.py` is the whole server: Python using only the standard library (Pillow is used if it is installed), with `roster_decode.py` beside it. It makes its own certificate authority and server certificate on first use; no other program is needed.

```text
python olserver.py run --install <install folder>         listen on 127.0.0.1:10000 and 10080
python olserver.py selftest --install <install folder>    codec and TLS round trip against itself
python olserver.py gen-certs --install <install folder>   make this install's certificate authority and server certificate
```

## Running it for an install

With `--install` the server uses the layout the launcher creates and nothing else:

| What | Where |
|---|---|
| The game folders | read from `data/games/CUSA35521/game.json`, the record the launcher writes at setup |
| State, certificates and keys, content cache | `data/profiles/default/server/` |
| Optional website catalogue import | `data/import-cache/` |
| Logs | `data/logs/` |
| Reply files and the message table | `replies/` and `tables/` beside `olserver.py` |

A missing record, a game folder that has moved, or missing reply files or table stop it with a one-line message. `run` creates the certificates on first use. The emulator is then started with `--show23-local-ca` pointing at `ca.pem` in the server data folder.

Without `--install` the file uses the folder layout of the development tree it was written in; that mode is for development only.

## Cards

In an install the cards come from the game folder: on first start (or with `python olserver.py build-roster --install <install folder>`) the server reads the roster files of the base game and writes the player records, looks, pitch data and one base card per player into the install's server data folder. A new player starts with a full 26-man squad of Common cards; the rest are earned with packs, exchanges and programs. An optional import of the public card catalogue can be placed in `data/import-cache/` and is used instead.

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
