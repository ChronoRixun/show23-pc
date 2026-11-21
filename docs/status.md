# Status

Last updated 2026-11-21, for release 0.1.0. "Seen in the game" means a person played it in the emulator and it behaved correctly. "Built" means the server answers for it and the answers pass tests, but nobody has played it yet.

## The game itself

| Area | State |
|---|---|
| Version 1.00, offline modes | Playable. Full exhibition games have been played. Menus run at 60 frames a second; a played game averages about 35, lower in busy views, with small stutters. Raising this is the current priority. |
| Version 1.21 (update applied), offline | Starts, reaches the main menu and draws an exhibition game. Not yet played through. |
| Version 1.21 with the local server | Not working yet: the sign-in changes are written for the 1.00 executable only. |
| Player models, crowd, lighting, uniforms | Correct, with one known exception: a team's custom-designed uniform does not draw, so a created team plays in a stock uniform. |
| Platforms | Windows only so far. |
| Release build of the emulator | The `release` branch, without the development diagnostics, is the build in 0.1.0. From a fresh install it signs in to the local server and every Diamond Dynasty menu has been visited; a full CPU game has not yet been played start to finish in this build. |

## Diamond Dynasty with the local server (version 1.00)

| Feature | State |
|---|---|
| Sign-in, creating a team, home screen | Seen in the game, also from a fresh 0.1.0 install: the player's chosen name, the 26-card Common starter squad and 5,000 Stubs |
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
| Special cards from the optional import, with their designed art | Seen in the game |
| Event packs (Chase Packs, Diamond Duos) from the optional import | Built: 40 packs listed; buying the last row of a long shop list is unchecked |
| Choice packs | Not supported |
| Captain cards | Unverified |
| XP reward path | Seen in the game |
| Team Affinity: six division programs with repeatable club tasks | The list and a division page open in the game; earning and rewards are built |
| Other Programs | Not built |
| Collections | Seen in the game (viewing) |
| My Inventory: cards, stadiums, equipment, sponsorships, unlockables, perks | Seen in the game (viewing) |
| Team Equipment: bats and socks | Seen in the game (pictures, equipping a bat); whether the bat shows on the field is unchecked |
| Exchanges: trading player cards for packs | Seen in the game. With cards from the game folder, any card outside a squad can be exchanged. |
| Community Market, buying Stubs | Deliberately disabled |
| Custom team logo | Not working |
| Playing against other people | Out of scope |

## The launcher (`show23-pc-launcher`)

| Part | State |
|---|---|
| Window, sidebar, Home, Settings, Mods, Support | Built and used in the 0.1.0 tests |
| Set up: an unpacked game folder, used where it is | Works: verified, its location recorded, nothing written inside it |
| Set up: a package file, through an unpacker program the player supplies | Works: tested on a real base package |
| Installing the bundled emulator build and server | Works |
| Play: starting and stopping the server and the emulator together | Works |
| Player name, asked at the first Play and kept in Settings | Works |
| Import special cards, with progress and cancel | Works: about 5 minutes and 35 MB for 4,101 catalogue cards |
| Self-update, Stable and Beta channels | Built, not yet tried against a real release |

## Where the cards come from

In an install the server builds the card pool from the game copy alone: 2,790 Live cards from the base game's roster, with player photos from the base game and, if set up, the 1.21 update. The optional special-card import adds about 1,775 special cards from the publisher's public website, fetched by the player's own PC; their designed pictures come mostly from the player's own 1.21 update files. About 211 special cards are left out because they could not be matched to a roster player. Nothing in the release contains game files or card art.
