# Local server

`olserver.py` is the whole server: Python using only the standard library (Pillow is used if it is installed; the release bundles it), with `roster_decode.py` and `card_import.py` beside it. It makes its own certificate authority and server certificate on first use; no other program is needed.

```text
python olserver.py run --install <install folder>         listen on 127.0.0.1:10000 and 10080
python olserver.py selftest --install <install folder>    codec and TLS round trip against itself
python olserver.py gen-certs --install <install folder>   make this install's certificate authority and server certificate
python olserver.py build-roster --install <install folder>  build the card pool and roster data from the game folder
python olserver.py import-cards --install <install folder> --pictures   optional special-card import
```

## Running it for an install

With `--install` the server uses the layout the launcher creates and nothing else:

| What | Where |
|---|---|
| The game folders | read from `data/games/CUSA35521/game.json`, the record the launcher writes at setup |
| State, certificates and keys, content cache | `data/profiles/default/server/` |
| Optional special-card import | `data/import-cache/` |
| Logs | `data/logs/` |
| Reply files and the message table | `replies/` and `tables/` beside `olserver.py` |

A missing record, a game folder that has moved, or missing reply files or table stop it with a one-line message. `run` creates the certificates on first use. The emulator is then started with `--show23-local-ca` pointing at `ca.pem` in the server data folder.

Without `--install` the file uses the folder layout of the development tree it was written in; that mode is for development only.

## Cards

In an install the cards come from the game folder: on first start (or with `build-roster`) the server reads the roster files of the base game and writes the player records, looks, pitch data and one base card per player (2,790 Live cards) into the install's server data folder, with an index of the player pictures found in the base game and, if set up, the 1.21 update. A new player starts with a full 26-man squad of Common cards and 5,000 Stubs; the rest are earned with packs, exchanges and programs.

`import-cards` is the optional special-card import the launcher's **Import special cards** button runs. It asks the publisher's public website for the card list (165 pages, at least 750 ms apart, generic user agent, retries with backoff) and, with `--pictures`, only the pictures neither game edition has. Pages and pictures go to `data/import-cache/<date>/`; an interrupted import resumes, and the server uses the newest complete one at its next start. Special cards are matched to roster players by name, team, jersey numbers, handedness, birthplace, size and age; about 1,775 are accepted and about 211 left out as unclear. Their designed art comes from the game folders by item id where present, otherwise from the imported picture (PNG and JPEG are converted to WebP when Pillow is available). An import also makes 40 event packs available in the shop (Chase Packs, Diamond Duos and the Lou Gehrig Day pack); an event pack appears only when all its featured cards were matched. Choice packs are not supported. Exit codes: 0 success, 2 network failure after retries, 1 anything else.

## What is here

| Path | Contents |
|---|---|
| `olserver.py` | Transport, message codec, and every request handler |
| `roster_decode.py` | Reads the game's roster files: player records, looks, pitch data, the disc card pool and picture index |
| `card_import.py` | The optional special-card import and the matching of special cards to roster players |
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
