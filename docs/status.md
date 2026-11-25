# Status

This page covers release 0.2.0. The Diamond Dynasty table also records earlier play checks from the emulator build.

## The game itself

| Area | State |
|---|---|
| Version 1.00 | Runs on the native game engine. A full CPU game has yet to be checked from start to finish in the release build. |
| Version 1.21 update | Optional source of card pictures. Play uses version 1.00. |
| Frame rate and player models | Higher in-game frame rate than on the emulator build. Distorted triangles on players in close-ups are fixed. First-use shader compilation can cause stutters. |
| Platforms | 64-bit Windows 10 and 11. Testing so far covers one PC with an NVIDIA graphics card. |
| In-game videos | Awaiting a retest on the native engine. |
| Trophies | Unsupported in this release. |

0.2.0 needs a fresh ZIP extraction and install folder. Saves and Diamond Dynasty progress start fresh, and the game shows its first-launch tutorial again. The previous release remains available for players who prefer the [0.1.x emulator build](https://github.com/ChronoRixun/show23-pc-emulator/tree/release).

## Diamond Dynasty with the local server (version 1.00)

In 0.2.0, Diamond Dynasty shows the server's fixed account name. The server leaves existing progress intact if a read fails and shows an unavailable message for Storylines.

In the table, "Seen in the game" refers to earlier emulator play checks. "Built" means the server answers passed tests. Special-card import and its in-game results have yet to be retested on the native engine.

| Feature | State |
|---|---|
| Sign-in, creating a team, home screen | Seen in the game from a fresh 0.1.0 install, with the 26-card Common starter squad and 5,000 Stubs |
| Squad management, card pictures | Seen in the game |
| Play vs. CPU, with the result, XP and Stubs kept | Seen in the game |
| Mini Seasons: schedule, games, standings, goals | Seen in the game. Playoff games are not built. |
| Conquest, first tutorial map: capturing, reinforcing, attacking by simulation or by playing | Seen in the game |
| Conquest: Steal Fans game, moving fans | Seen in the game |
| Conquest: other maps | Not built |
| Showdown, starter entry: free entry, ten-round draft, a moment, defeat | Seen in the game |
| Showdown: winning a moment, upgrade picks, rallies that start with a runner on second | Built |
| Showdown: moments that can be retried after a loss | Not built |
| Pack shop: buying with Stubs, opening, the card reveal | Seen in the game |
| Special cards from the optional import, with their designed art | Seen in the game in 0.1.x. Awaiting a native retest. |
| Event packs (Chase Packs, Diamond Duos) from the optional import | Built: 40 packs listed; buying the last row of a long shop list is unchecked |
| Choice packs | Not supported |
| Captain cards | Unverified |
| Storylines | Unavailable on the local server |
| XP reward path | Seen in the game |
| Team Affinity: six division programs with repeatable club tasks | The list and a division page open in the game; earning and rewards are built |
| Other Programs | Not built |
| Collections | Seen in the game (viewing) |
| My Inventory: cards, stadiums, equipment, sponsorships, unlockables, perks | Seen in the game (viewing) |
| Team Equipment: bats and socks | Seen in the game (pictures, equipping a bat); whether the bat shows on the field is unchecked |
| Exchanges: trading player cards for packs | Seen in the game. With cards from the game folder, any card outside a squad can be exchanged. |
| Community Market, buying Stubs | Deliberately disabled |
| Custom team logo | Not working |
| Playing against other people | Unavailable |

## The launcher (`show23-pc-launcher`)

| Part | State |
|---|---|
| Set up from unpacked game folders | Verifies the folders and records their locations for use in place |
| Set up from packages | Uses the player's unpacker, then verifies and adopts the extracted folders |
| Runtime | Setup stages the bundled native engine and server. Verify Files checks them. Repair runtime uses a matching bundle. |
| Play | Prepares files on the first launch, then runs the local server and native engine. Later launches are quicker. The Windows command-line wrappers use the same install. |
| Settings | Local player name, 720p or 1080p window, fullscreen at desktop size |
| Import special cards | In 0.1.x it took about 5 minutes and 35 MB for 4,101 catalogue cards. Awaiting a native retest. |
| Updates | Stable and Beta list native releases. A 0.1.x launcher cannot update to 0.2.0 in place. |

## Where the cards come from

In an install the server builds the card pool from the game copy alone: 2,790 Live cards from the base game's roster, with player photos from the base game and, if set up, the 1.21 update. The optional special-card import adds about 1,775 special cards from the publisher's public website, fetched by the player's own PC; their designed pictures come mostly from the player's own 1.21 update files. About 211 special cards are left out because they could not be matched to a roster player. Nothing in the release contains game files or card art.
