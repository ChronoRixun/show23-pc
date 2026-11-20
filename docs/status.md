# Status

Last updated 2026-11-20. "Seen in the game" means a person played it in the emulator and it behaved correctly. "Built" means the server answers for it and the answers pass tests, but nobody has played it yet.

## The game itself

| Area | State |
|---|---|
| Version 1.00, offline modes | Playable. Full exhibition games have been played. Menus run at 60 frames a second; busy in-game views run at about 30. |
| Version 1.21 (update applied), offline | Starts, reaches the main menu and draws an exhibition game. Not yet played through. |
| Version 1.21 with the local server | Not working yet: the sign-in changes are written for the 1.00 executable only. |
| Player models, crowd, lighting, uniforms | Correct, with one known exception: a team's custom-designed uniform does not draw, so a created team plays in a stock uniform. |
| Platforms | Windows only so far. |

## Diamond Dynasty with the local server (version 1.00)

| Feature | State |
|---|---|
| Sign-in, creating a team, home screen | Seen in the game |
| Squad management, card pictures | Seen in the game |
| Play vs. CPU, with the result, XP and Stubs kept | Seen in the game |
| Mini Seasons: schedule, games, standings, goals | Seen in the game. Playoff games are not built. |
| Conquest, first tutorial map: capturing, reinforcing, attacking by simulation or by playing | Seen in the game |
| Conquest: Steal Fans game, moving fans | Built |
| Conquest: other maps | Not built |
| Showdown, starter entry: free entry, draft, a moment, defeat | Seen in the game |
| Showdown: winning a moment, upgrade picks, ten-round draft | Built |
| Showdown: moments that can be retried after a loss | Not built |
| Pack shop: buying with Stubs, opening, the card reveal | Seen in the game |
| XP reward path | Seen in the game |
| Team Affinity: six division programs with repeatable club tasks | Built; the list screen does not open in the game yet (known issue) |
| Other Programs | Not built |
| Collections | Seen in the game (viewing) |
| My Inventory: cards, stadiums, equipment, sponsorships, unlockables, perks | Seen in the game (viewing) |
| Team Equipment: bats and socks | The screen opens in the game; card pictures and equipping are built |
| Exchanges: trading spare player cards for packs | The lists open in the game; completing a trade is built |
| Community Market, buying Stubs | Deliberately disabled |
| Custom team logo | Not working |
| Playing against other people | Out of scope |

## Where the cards come from

Today the development setup builds the card list from two sources on the developer's machine: the roster files of the game copy, and a saved copy of the public card catalogue from the game's website. A release must work from the game copy alone, with the website import as an optional extra. That work is the first item in [roadmap.md](roadmap.md).
