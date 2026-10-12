# Software drivers (bots) — the `C` and `B` commands

A **software driver** ("bot") holds the throttle of a controller slot (addresses
0-5) instead of a hand controller. The CU keeps sending that throttle on the
track at its normal rate (each slot refreshed every ~75 ms), so the car behaves
exactly as if someone held a controller.

RaceLab can reach the cars two ways. Both give the same result on the track and
the same per-slot model in the app (human / bot at throttle *n* / lane change);
RaceLab picks one by itself (`bot_transport = "auto"` in the
[configuration](../racelab/racelab.example.toml)):

| | `C` — stock CU | `B` — bot firmware module |
|---|---|---|
| CU firmware | the stock V339 firmware, as sold | a `v339-bot.N` image ([firmware/](../firmware/README.md)) |
| firmware update | **none** | needed once |
| one frame drives | all 6 slots | one slot |
| app goes silent | after ~750 ms every slot is back on its controller | after ~1 s bot slots are held at throttle 0 (bot.5+) |
| RaceLab uses it | first choice on a V339 CU | when `C` is not available and the module answers |

For the general frame format (framing, nibbles, checksum), see
[protocol.md](protocol.md).

## `C` — the stock CU's host throttle table

The stock V339 firmware keeps a small table, one entry per controller slot. On
each rail cycle it reads that entry **before** the controller: a slot with a host
value transmits the host value; a slot without one transmits its controller.
Everything downstream is the CU's own code, so the start-light hold, false-start
detection and fuel apply to bots exactly as to hand controllers. The `C` command
fills the whole table at once.

### Format

```
C <t0><l0> <t1><l1> <t2><l2> <t3><l3> <t4><l4> <t5><l5> <checksum>
```

Serial: `"C…$`. BLE: one 14-byte write. The checksum is the usual one (sum of
the characters after `C`, modulo 16, as a nibble).

- `t` — throttle character for the slot:

| `t` | Effect |
|---|---|
| `0` | **no host value**: the slot's controller drives |
| `1`-`9`, `:` `;` `<` `=` `>` `?` | **bot throttle** 1-15 |
| `P` | **bot throttle 0**: the car is held still and the controller is ignored |

- `l` — lane character: `1` = the slot's lane-change button is pressed, `0` = not
  pressed. The press lasts until the next frame.

The CU answers `C`.

### Expiry

Each `C` frame reloads a countdown of 60 controller words, i.e. 10 rail cycles
(about 750 ms). When it runs out, the CU clears the whole table: **every slot is
back on its controller**. While a bot is active the application must therefore
send a frame well within 750 ms, even if nothing changed (RaceLab: every 300 ms,
and at once on any change).

### From the bot model to a frame

| Slot | Characters |
|---|---|
| human (not a bot) | `00` |
| bot, throttle 0 | `P0` |
| bot, throttle *n* (1-15) | `'0'`+*n*, then `0` |
| lane change | `1` as the lane character for one frame (RaceLab holds it 120 ms, then sends the frame again without it) |
| hand back | `00` |

Examples (without framing):

- `C008150<0P0?09`: slot 0 human, slot 1 throttle 8 changing lane, slot 2
  throttle 5, slot 3 throttle 12, slot 4 held at 0, slot 5 throttle 15
- `C0000000000000`: every slot back on its controller

### Detection

RaceLab sends `C` only to a CU whose version answer (command `0`) ends in `339`.
If that CU rejects `C` (answers `#`), RaceLab tries the `B` module, and without it
bots are unavailable on that CU. Older firmware versions are not used with `C`:
their table has not been checked.

## `B` — the bot firmware module

A `v339-bot.N` image (built from [firmware/](../firmware/README.md)) adds one
serial command, `B`, that drives one slot per frame. It is kept for CUs where
`C` is not available and as the base for features to come.

### Format

```
B<address><value>
```

On a serial line the frame is wrapped as `"B<address><value>$`. There is no
checksum.

- `address`: controller address 0-5, one character (`'0'`-`'5'`)
- `value`: one character, value = character − `0x30`:

| `value` | Effect |
|---|---|
| `0`-`9`, `:` `;` `<` `=` `>` `?` (0-15) | **bot throttle** = 0-15 (turns bot mode on for that address) |
| `A` | **hand back to the physical controller** (the slot stops being a bot) |
| `L` | **one-shot lane change**: the slot's lane-change bit is set for a single transmission of the controller word, then falls back by itself (equivalent to a 75 ms press of the controller's lane-change button) |

Any other value is reserved: applications must not send it.

Examples (without framing):

- `B07`: slot 0, **throttle 7** (turns bot mode on)
- `B1:`: slot 1, **throttle 10** (`:` = 10)
- `B0L`: slot 0 **changes lane**
- `B0A`: slot 0 **hands back** to the physical controller

### Semantics

- **Bot priority**: while bot mode is on for an address, the CU transmits the
  bot's throttle and **ignores any controller plugged into that slot**.
  Sending `A` hands control back immediately.
- **Lane change**: the bit travels in the slot's controller word, like a press
  of the controller's button. The car applies it when it crosses a switch
  area. It is one-shot by design: the application never sends a "release".
  The request is ignored if the address is not currently in bot mode.
- **Persistence**: the throttle stays as it is until the next command, within
  the firmware watchdog below.
- **Throttle 0 is not "off"**: `B<address>0` keeps the bot in control of the
  slot at throttle 0; only `A` hands the slot back.
- **Echo and detection**: a CU with the module echoes `B`; a CU without it
  answers `#` (command not understood). `B0A` is therefore a harmless probe: it
  hands slot 0 back, which changes nothing if it is not a bot. RaceLab sends it
  at startup to know whether the module is there.
- **Firmware watchdog** (v339-bot.5 and later): a bot slot that receives no
  throttle command for about one second (13 rail cycles) gets throttle 0 and
  stays a bot. RaceLab resends each active bot's throttle every 500 ms.

## Safety rules for applications (both commands)

1. **Heartbeat.** Resend the active bots' throttle well within the CU's limit
   (`C`: 750 ms, `B`: 1 s), and on reconnection after a drop force throttle 0
   before resuming.
2. **On exit.** Hand every slot back (`C0000000000000`, or `B<address>A`), or
   hold the active bots at throttle 0 (`P0`, or `B<address>0`).
3. **Emergency stop.** A STOP button = throttle 0 on every active bot (`P0` /
   `B<address>0`): the bots keep their slots, stopped. Handing back would give
   the cars to whoever holds a controller.
4. **What a silent application leaves behind** differs: with `C` the cars go
   back to their controllers (a car with no controller plugged in stops), with
   `B` they stop and stay bots. Keep a hand near the track's power supply during
   first tests.

## What the application should show per slot

- Mode (human / bot), current throttle, lane-change request in progress
- The bots' fuel levels come from the status frame (`?:`), like those of human
  drivers
- The bots' lap times come from the timer events (address = slot)

RaceLab implements all of this, and its virtual CU implements both commands so
that applications can be developed and tested without hardware. The rules
above are also summarised, from the user's side, in the
[user guide](user-guide.md#bot-safety--what-the-app-guarantees).
