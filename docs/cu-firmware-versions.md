# CU firmware versions — V336 to V339

What changed between the official Control Unit firmware versions, and what it means
for RaceLab. The four images compared are the ones the official Carrera app carried
(V336, V337, V338 "OVERFLOW", V339). The last app version flashes V339 to any CU that is
not on 339; its V338 entry is commented out.

Method: the images were disassembled and aligned instruction by instruction, with
registers and RAM addresses masked; V339 function names come from the
[reconstruction](../firmware/reconstruction/README.md). Each change below was then read
in the disassembly of both versions. Everything else that differs is RAM layout,
register allocation or compiler output (V339 was built with another compiler: it is
276 bytes smaller for the same logic).

| Version | `C` (bots on a stock CU) | What changed |
|---|---|---|
| V336 | no | — |
| V337 | no | timer events keep their own time; performance measuring mode |
| V338 | **yes** | the `C` command; an overflow lamp on the start lights |
| V339 | **yes**, identical | lamp removed; stricter lap-sensor filtering; track fault cut at once |

## V336 → V337

- **Timer events carry their own data.** V336 queued only the car number of each lap or
  sector event and built the `?` answer from that car's *latest* time. Two events of the
  same car pending between two polls came back with the same (later) time: the first
  was lost, the second duplicated. V337 queues a record per event (car, 32-bit time,
  sector). The frame on the wire is unchanged; the queue still holds 8 events.
- **Performance measuring mode.** `J` word 21 at address 7 (`"J5?116$` on, `"J5?015$`
  off) sets a flag: each pulse on lap-sensor input A is reported as an event of address
  0, on input B of address 1, without lap counting, best laps, beeps or position tower.
  This is the slotbaer "Performance-Messmodus"; the official app turns it on for its
  performance measurement.

## V337 → V338 ("OVERFLOW")

- **The `C` command** (see [bot-protocol.md](bot-protocol.md)): a host throttle table
  for the 6 controllers, read before the controllers, cleared about 750 ms after the
  last frame.
- **Overflow lamp.** Before storing a timer event, V338 checks whether the queue slot
  still holds an unread event; if so it lights start-light output PORTA.5 and leaves it
  on. The event is still overwritten, as in V337. This instrumentation is most likely
  what the file name refers to.

## V338 → V339

- **Overflow lamp removed.**
- **Two events of one car must be at least 501 ms apart** (101 ms in V336-V338); a
  closer one is ignored for laps, sectors and best times.
- **Lap-sensor re-arm is stricter**: 5 ticks instead of 3, and only once the sensor input
  is idle again.
- **Track power fault cut at the first bad sample** (V336-V338 waited for two).
- **Keep-awake setting respected** by the fault / power-down light countdown (likely).
- Ranking, position tower, fuel, pit lane, buttons, start lights, rail words, the `C`
  command and firmware-update decoding behave the same.

## What it means for RaceLab

- Bots on a stock CU need V338 or V339; RaceLab sends `C` only to those (checked:
  the V336/V337 command dispatcher has no `C`). A V336/V337 CU can be updated to the
  stock V339 in System → Advanced options.
- On V336, two laps of the same car between two polls can be reported wrong. RaceLab
  polls far more often than a car laps, so it should not show in practice.
- On V339, a car crossing a sensor twice within 0.5 s counts once.

## Notes for firmware work

- In every version the update frame decoder (`update_frame_decode`) trusts the length
  byte of a frame: a length of 17 or more writes past its 17-byte buffer. Not fixed by
  Carrera; worth a check in a modified firmware.
- PORTA.4/5 drive start-light outputs; V338 shows that they can serve as a debug lamp.
