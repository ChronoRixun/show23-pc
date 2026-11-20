# The game's online protocol, in brief

These are the facts the local server is built on. They were worked out by observing what the game sends to a server on the same machine and by reading how the game parses the answers.

## Transport

- TLS on port 10000. The game checks the server certificate against a list of authorities it carries; in local mode the emulator makes it trust the per-install authority instead.
- Inside TLS, messages travel in chunks; a chunk can be compressed.
- A message is `[u32 total length, including these 8 bytes][u32 type id][body]`, little-endian.
- A string is a `u32` length and, if not empty, the length again as a base-128 varint and then the bytes. A block of bytes is a `u32` length and the bytes.
- The type ids and names of the message kinds are in [`server/tables/ol-messages.csv`](../server/tables/ol-messages.csv).

## Service requests

Almost everything the card mode does is a *service request*: a name of the form `group.action` (for example `arena_team.view`) with a JSON object of parameters. The answer is XML:

```xml
<result>
  <status><id>0</id><message>OK</message></status>
  <response> ... </response>
</result>
```

- `status/id` 0 is success. Any other value makes the game show an error box with the message, and for many screens it then leaves the card mode. Where a feature is not available the server therefore prefers a valid, empty answer over an error.
- Values are attributes of `<response>` and of its child elements.
- The game looks names up by hash. Element and attribute names use `h = h * 131 + c`; names used by the menu scripts use a rotate-left-by-8 and XOR per lower-cased character, with the top bit set. `tools/ol-namehash.py` computes the first.
- Numbers the game formats itself must be sent bare: a bonus is `9`, not `+9`.
- A screen can fail on a missing child element or an attribute it expects to be a number, so answers are sent complete even when most values are zero.

## Game results

A finished game is uploaded as one attachment (`mlb_game.create`). It carries the game type, the outcome, both sides' totals and a text play-by-play. The server records each upload once, by its digest, because the game reuses one identifier for every game.

## Pictures

The game asks the server which downloaded pictures exist (a list of texture names with a checksum and an address) and fetches each as a small ZIP over plain HTTP on loopback. The server builds those ZIPs on the fly from pictures in the player's own game copy.
