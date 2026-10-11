# Software drivers (bots) — the `B` command

A custom CU firmware can add one serial command, `B`, that turns a controller
slot into a **software driver**: the throttle of the slot is held by the CU
instead of a hand controller. No controller is needed; the CU keeps sending
the throttle on the track at its normal rate (each slot refreshed every
~75 ms).

> **The firmware that implements this command is not part of this
> repository.** The original Carrera firmware is not free software and cannot
> be redistributed, so neither can a firmware derived from it. This page is
> the interface specification: RaceLab and carreralib implement the host side,
> and RaceLab's virtual CU implements the device side so that applications can
> be developed and tested without hardware. Without a CU that implements `B`,
> RaceLab runs in Classic mode only; see the [user guide](user-guide.md).

For the general frame format (framing, nibbles, checksum), see
[protocol.md](protocol.md).

## Format

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

## Semantics

- **Bot priority**: while bot mode is on for an address, the CU transmits the
  bot's throttle and **ignores any controller plugged into that slot**.
  Sending `A` hands control back immediately (the controller is in charge
  again).
- **Lane change**: the bit travels in the slot's controller word, like a press
  of the controller's button. The car applies it when it crosses a switch
  area. It is one-shot by design: the application never sends a "release".
  The request is ignored if the address is not currently in bot mode.
- **Persistence**: the throttle stays as it is until the next command, so the
  application does not need to repeat the value every 75 ms. But see the safety
  rules below.
- **Throttle 0 is not "off"**: `B<address>0` keeps the bot in control of the
  slot at throttle 0; only `A` hands the slot back.
- **Ghost car and pace car** (addresses 6-7) are not covered by `B` (v1): bots
  live on addresses 0-5.
- **Echo and detection**: a CU that implements `B` echoes the letter `B`. A CU
  with stock firmware answers `#` (command not understood). `B0A` is therefore a
  harmless probe: it hands slot 0 back, which changes nothing if it is not a
  bot. RaceLab uses it at startup: bot control is only enabled when the CU does
  not reject it.

## Safety rules for applications (IMPORTANT)

1. **Throttle watchdog.** If the application crashes, closes, or loses
   Bluetooth, the firmware keeps the last throttle. The application **MUST send
   `B<address>0` for every active bot address when it closes**, should resend
   the throttle periodically as a heartbeat (RaceLab: every 500 ms), and on
   reconnection after a drop must force throttle 0 before resuming.
2. **Lane-change polarity.** At rest the lane-change bit of the controller word
   is 1 (not pressed). The firmware handles the polarity: the application only
   sends "change lane" with `L`.
3. **Emergency stop.** A STOP button in the application = `B<address>0` on
   every active bot (throttle 0 is not "bot off": the bot keeps the slot at
   throttle 0).
4. **Do not confuse** `B<address>A` (hand back) with `B<address>0` (bot active,
   stopped).

A firmware-side guard (throttle 0 when no `B` command has arrived for about
one second) would close the one hole no application can cover: a process that
is killed abruptly, or a link that drops and never comes back. Firmware
implementers are encouraged to add it; until then, keep a hand near the track's
power supply during tests.

## What the application should show per slot

- Mode (human / bot), current throttle, lane-change request in progress
- The bots' fuel levels come from the status frame (`?:`), like those of human
  drivers
- The bots' lap times come from the timer events (address = slot)

RaceLab implements all of this; the rules above are also summarised, from the
user's side, in the [user guide](user-guide.md#bot-safety--what-the-app-guarantees).
