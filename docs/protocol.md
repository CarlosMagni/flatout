# CU serial / Bluetooth protocol

How an application talks to the Control Unit (CU) of a Carrera Digital 124/132
track, over a serial cable or through the Carrera AppConnect Bluetooth dongle.

This is not an official Carrera specification. It collects what the public
carreralib project and the community documentation (slotbaer.de) describe, plus
the project's own observations. It may be incomplete. The
reference implementation is [`carreralib/`](../carreralib)
([`protocol.py`](../carreralib/src/carreralib/protocol.py),
[`cu.py`](../carreralib/src/carreralib/cu.py)).

The one extension that is specific to this project, the `B` command (software
drivers), is specified separately in [bot-protocol.md](bot-protocol.md).

## 1. Transports

| | |
|---|---|
| Serial | 19200 baud, 8N1, on the CU's serial port (USB-serial adapter) |
| Bluetooth LE | Carrera AppConnect: a BLE-to-serial bridge plugged into the CU's serial port (19200 8N1 behind the bridge) |

Bluetooth LE parameters:

| Item | Value |
|---|---|
| Advertised name | `Control_Unit` |
| Service UUID | `39DF7777-B1B4-B90B-57F1-7144AE4E4A6A` |
| Write characteristic (host → CU) | `39DF8888-B1B4-B90B-57F1-7144AE4E4A6A` |
| Notify characteristic (CU → host) | `39DF9999-B1B4-B90B-57F1-7144AE4E4A6A` |
| Firmware update block size | 18 bytes at most |

The AppConnect accepts one BLE connection at a time: the phone app must be
closed before another program connects.

## 2. Frames (host → CU)

```
serial:  "  <letter> <parameters> [<checksum>]  $        (0x22 ... 0x24)
BLE:        <letter> <parameters> [<checksum>]           (one GATT write, no delimiters)
```

The examples below omit the framing and show `<letter><parameters><checksum>`.

**Parameters** are ASCII characters that encode *nibbles*: character =
`'0'` + value, so 0-9 are `0`-`9` and 10-15 are `:` `;` `<` `=` `>` `?`.

| Notation | Meaning |
|---|---|
| `Y` | one nibble (0..15), one character |
| `B` | one byte, two characters: low nibble first, then high nibble |
| `I` | 32-bit unsigned integer, eight characters: the four bytes most significant first, each written as `B` |
| `c` | one literal ASCII character |
| `r` | one raw byte (not nibble-encoded) |
| `C` | checksum, see below |

**Checksum**: the sum of the ASCII codes of everything after the command
letter, modulo 16, encoded as a nibble (`'0'` + sum & 0x0F). Example: `J6091`
→ `0` → `J60910`. The `$` delimiter is not part of the sum. Commands without a
checksum: `0`, `?`, `B`, and the `F` blocks of a firmware update.

Examples, as sent on a serial line (with framing):

| What | Frame |
|---|---|
| version request | `"0$` |
| poll | `"?$` |
| press the Start button | `"T22$` |
| clear the position tower display | `"J60910$` |
| show car 2 in position 1 on the position tower | `"J6411<$` |
| set the maximum speed of car 2 to 9 (repeat 2) | `"J0492?$` |
| reset the CU timer | `"=101$` |
| bot, slot 1, throttle 10 (custom firmware) | `"B1:$` |

## 3. Responses (CU → host)

On a serial line a response is the bytes up to a `$`, or up to a `#`. Three
things to know:

- **Echo.** The CU answers a command with the command letter, which is how a
  host pairs a response with its request. A response of `#` (or an empty one)
  means the CU did not understand the command: the stock firmware answers `#`
  to `B`.
- **Spontaneous frames.** Status and timer frames (below) can arrive while the
  host waits for the answer to another command. A client must classify every
  received frame by its content, not assume it answers the last request.
- **BLE.** Notifications that end with `$` do not carry the leading command
  letter. carreralib puts it back (`0` for a 6-byte notification, which is a
  version answer, `?` for any other), so both transports look the same to
  the caller. After that, an echo-only answer to a command `X` can look like
  `X` or `?X`; RaceLab accepts both.

### 3.1 Version (answer to `0`)

`0` + 4 characters + checksum, e.g. `05339` + `C`: hardware `5`, firmware
`339`.

### 3.2 Status (answer to `?` when no timer event is pending)

`?:` followed by:

| Offset | Format | Content |
|---|---|---|
| 0..7 | 8 × `Y` | fuel level of addresses 0..7 (0..15) |
| 8 | `Y` | start phase (below) |
| 9 | `Y` | mode bit mask: 1 = fuel mode, 2 = real fuel mode, 4 = pit lane adapter connected, 8 = lap counter connected |
| 10..11 | `B` | pit lane bit mask: bit *n* = address *n* is in the pit lane |
| 12 | `Y` | display type: 6 or 8 drivers |
| 13..14 | | two extra bytes sent by recent CU versions (meaning unknown) |
| last | `C` | checksum |

Start phase: 0 = race / idle (lights off), 1 = start armed (5 red lights),
2..6 = countdown (1 to 5 lights on), 7 = lights out (GO), 8..9 = false start.

### 3.3 Timer event (answer to `?` when one is pending)

`?` + `Y` address + `I` timestamp + `Y` sector + `C` — 12 characters.

- address: the controller address **plus 1** on the wire (1..8 for addresses
  0..7);
- timestamp: CU clock in milliseconds, 32 bits, wraps around: a lap time is the
  difference of two timestamps modulo 2³²;
- sector: 1 = start/finish line, 2 and 3 = Check Lanes (intermediate times).

Example: `?20010;9?41;` is address 1, timestamp 105295 ms, sector 1.

## 4. Commands

| Letter | Frame | Effect |
|---|---|---|
| `0` | `0` | version request |
| `?` | `?` | poll: returns a pending timer event, otherwise the status |
| `J` | `J` `B`(word \| address<<5) `Y`(value) `Y`(repeat) `C` | **programming word** (below) |
| `T` | `T` `Y`(button) `C` | simulate a CU button: 1 = Pace/ESC, 2 = Start/Enter, 5 = Speed, 6 = Brake, 7 = Fuel, 8 = Code |
| `:` | `:` `B`(mask) `C` | ignore controllers: bit *n* set = address *n* is excluded from timing |
| `=` | `=` `Y`(1) `Y`(0) `C` | reset the CU timer (`=101`) |
| `B` | `B` `c`(address) `c`(value) | software drivers, custom firmware only: see [bot-protocol.md](bot-protocol.md) |
| `G`, `E`, `F` | | firmware update: see section 7. **Never send them otherwise.** |
| `I`, `C` | | exist on the CU (CU information, CU settings); not used by RaceLab or carreralib, not documented here. RaceLab refuses to send `C`. |

### Programming words (`J`)

`J` takes a 5-bit word number and an address (0..7) packed in one byte
(`word | address << 5`), a value 0..15 and a repeat count 1..15 (how many
times the word is repeated on the track).

| Word | Meaning | Address | Value |
|---|---|---|---|
| 0 | maximum speed of a car | car | 0..15 (repeat 2) |
| 1 | brake strength of a car | car | 0..15 (repeat 2) |
| 2 | fuel tank size of a car | car | 0..15 (repeat 2) |
| 6 | position tower: car in a given position | car | position 1..8; value 9 with address 0 clears the display |
| 17, 18 | lap counter, high and low nibble of the lap number | 7 | 0..15 |
| 3, 4, 7 | reset, normal operation, chequered flag | | not used by RaceLab |

## 5. Addresses

| Address | Who |
|---|---|
| 0-5 | the 6 controllers (wired or Wireless+, the CU does not tell them apart) |
| 6 | autonomous car (ghost car) |
| 7 | pace car |

Software drivers (the `B` command) only exist on addresses 0-5. Timer events
report the address of the car that crossed, plus 1 on the wire.

## 6. Timings

- The CU refreshes each controller's throttle on the track once per cycle of
  about 75 ms, whether or not a controller is plugged in. The latency from a
  command to the car is therefore up to about 75 ms plus the link latency.
- Programming is slow: about 150 ms per operation (one word per 75 ms cycle,
  sent twice). Refreshing the position tower for 8 cars takes about 1.2 s.

## 7. Firmware update

Described by carreralib's `fw.py` and RaceLab's *System → CU firmware* panel.
Firmware images are `.HMF` files; the V339 images used by this project are in
built from source into `firmware/build/` (tags and SHA-256 in `firmware/manifest.json`).

1. `GB` (+ checksum, `GB2`): the CU restarts in its bootloader. Wait about 2 s.
2. For each line of the `.HMF` file (with its surrounding double quotes
   removed): on serial, `E` + the line + checksum; over BLE, the line is cut
   into blocks of at most 18 bytes, each sent as `F` + one raw byte (the block
   length) + the block, followed by `E` + checksum to commit the line.
3. After the last line the CU restarts on the new firmware.

If the process is interrupted, the CU stays in its bootloader; the image has to
be sent again from the first line (without `GB`).

## 8. Warnings

- **Never send `GB` outside a firmware update.** It restarts the CU in its
  bootloader, where it no longer behaves as a normal CU until a complete image
  has been sent. The same goes for `E` and `F`.
- Do not send `C`.
- Programming words 0, 1 and 2 **reprogram cars**, which remember the values
  permanently. Use them only from a setup screen, one car at a time on the
  track, never during a race.
- The programming channel is slow (section 6): coalesce updates instead of
  queueing them.
- RaceLab enforces the first two rules itself: `G`, `E` and `F` are only sent
  in firmware-update mode, `C` never.
