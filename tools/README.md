# Developer tools

Read-only helpers. None of them changes a game file.

| Tool | Use |
|---|---|
| `pkg-info.py <file.pkg>` | Prints what a PS4 package's unencrypted header says: content id, type, sizes, and title and version if readable. Decrypts nothing. |
| `roster-decode.py [game folder] [output]` | Decodes the player records in the game's roster files. Like the server, it still defaults to the development tree's folder layout. |
| `ol-namehash.py <name>` | The hash the game's reply parser uses for element and attribute names. |
