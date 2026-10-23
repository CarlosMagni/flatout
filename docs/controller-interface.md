# Controller interfaces — bots on any CU

RaceLab's software drivers (bots) normally reach the cars through the CU itself: its `C`
table (stock firmware V338/V339) or the bot firmware module's `B` command
([bot-protocol.md](bot-protocol.md)). Some CUs take neither: a hardware-8 CU (STM32) ignores
both. A **controller interface** is hardware on one of the CU's controller sockets that plays
the hand controllers instead. The CU only sees controllers, so start lights, false starts and
fuel apply to bots exactly as to people, on any CU, with no firmware involved.

```
RaceLab ──serial (USB, USB-serial, Bluetooth serial)──▶ your device ──controller socket──▶ CU ──rails──▶ cars
   └──────── CU link (AppConnect or serial cable): timing, start lights, race state ────────┘
```

RaceLab drives the interface through a link of its own, apart from the CU link. A fader step
or a lane press reaches the device within ~30 ms, whatever the CU link is doing (each CU poll
waits ~50 ms over the AppConnect).

Anything that speaks the small wire protocol below works, with no code to add to RaceLab. The
reference implementation is [`controller_interfaces/pico/`](../controller_interfaces/pico/README.md):
a Raspberry Pi Pico and an HC-05 Bluetooth module, which plays the 6 controllers on socket 1
like a WIRELESS+ receiver. It runs on a hardware-8 CU (v8339).

## The wire protocol

A byte stream that RaceLab opens as a serial port: USB (CDC), a USB-serial adapter, a Bluetooth
serial (SPP) module. ASCII, 8N1. The rate only matters on a real UART: it is set with
`[interface] baud`, 115200 by default. USB and Bluetooth serial ignore it.

### Frames: RaceLab → device

The CU's own `C` frame ([bot-protocol.md](bot-protocol.md)), wrapped as on the CU's serial
line:

```
"C<t0><l0><t1><l1><t2><l2><t3><l3><t4><l4><t5><l5><checksum>$
```

| Field | Meaning |
|---|---|
| `t` | controller 1..6: `0` = **not driven** (the socket's real controller drives), `P` = throttle 0, `1`-`9` `:` `;` `<` `=` `>` `?` = throttle 1-15 |
| `l` | `1` = the lane button is held, `0` = released |
| checksum | `'0'` + (sum of the 12 characters `t0`…`l5`) mod 16 |

Example: `"C008150<0P0?09$` leaves 1 alone; drives 2 at 8 with the lane button held, 3 at 5,
4 at 12, 5 at throttle 0 (held still), 6 at 15.

Timing, as RaceLab sends it:
- a change goes out at once (two frames at least 20 ms apart);
- an unchanged frame is repeated every 100 ms while any slot is driven;
- a lane press is held at least 120 ms, longer than one rail cycle (~75 ms);
- on exit, a last frame hands every slot back (`0` everywhere), or holds the bots at `P`
  (`[safety] release_on_exit`).

**What the device must do:**
1. Drive each slot as the latest *valid* frame says, and ignore anything that does not check
   out: wrong length, unknown character, bad checksum.
2. **Hand every slot back when no valid frame has come for 750 ms.** It is the CU's own rule
   for its `C` table, and the safety net if RaceLab, the computer or the link dies.
3. Treat a lane press as the controller's button: held while `l` is `1`.

### Reports: device → RaceLab (optional, recommended)

One line a second, the device's name and then `key=value` pairs:

```
pico sync=on period=73.52ms peak=17.6V bursts=1141 frames=3/s words=13/s late=0 baud=115200 slots=---0--
```

- RaceLab shows the pairs in System → Connection, as they come. The keys are yours.
- After the first report, **3 s without one means the link is lost**. RaceLab then holds the
  bots at 0, closes the port, opens it again, and the bots restart from 0. A device that never
  reports is never watched this way, so plain one-way hardware still works.

### Device-specific lines

RaceLab sends nothing but frames. Lines starting with `#` are free for your device's own use,
for instance live tuning from a terminal. The reference Pico takes `#shift=<us>`,
`#repeat=1|2` and `#presence=0|1`.

## In RaceLab

- **Choose it:** System → Connection → *Controller interface* → *Search ports…* → *Use*.
  A Bluetooth module that is not paired yet appears below the ports, with *Pair and use*:
  on macOS, RaceLab pairs it itself through [blueutil](https://github.com/toy/blueutil)
  (`brew install blueutil` once). You can also set it in the file: `[interface] port`.
- **Next to a real CU only:** the virtual CU drives its own cars.
- **All the bots go through it** while it is set (link field `bot_transport: "I"`). The CU link
  keeps the timing and the race state; nothing is sent to the CU's `C` table.
- **Safety, the same as through the CU:** E-STOP first; throttle 0 and a hold when the
  interface or the CU link is lost (the bots restart from 0 once it is back); the release
  frame on exit; the device's 750 ms expiry underneath.
- **API:** `interface.scan`, `interface.set`, `interface.bt_scan`, `interface.pair`, and the
  `interface*` link fields ([api.md](api.md)).

## Building your own

### Hardware: no code in RaceLab

1. Read the frames from a serial port, and check them (length, characters, checksum).
2. Implement the 750 ms expiry.
3. Make the CU see controllers on the socket side. The reference Pico sends the digital words
   a WIRELESS+ receiver sends on contact 1 of socket 1, synced on the CU's burst on contact 6;
   the socket pinout, voltages and word format are in its
   [README](../controller_interfaces/pico/README.md). Driving a wired controller's analog
   throttle pin instead would also work, for sockets 1-4.
4. Optionally, send a report line once a second.

Test it without RaceLab: send frames from Python and watch the car with its wheels in the air.

```python
import serial, time
port = serial.Serial("/dev/cu.your-device", 115200)

def frame(slots):  # 6 x (throttle 0-15 or None, lane button bool)
    body = "".join(("0" if t is None else "P" if t == 0 else chr(48 + t)) + "01"[lane] for t, lane in slots)
    return ('"C' + body + chr(48 + sum(map(ord, body)) % 16) + "$").encode()

while True:  # controller 1 at throttle 5, the others not driven: resend well within 750 ms
    port.write(frame([(5, False)] + [(None, False)] * 5))
    time.sleep(0.1)
```

### Another transport: a few lines of Python

The protocol does not have to travel on a serial port. Subclass
`racelab.drive.ControllerInterface` and implement four methods. The base class brings the bot
API, the safety rules, the frame timing and a thread of its own.

```python
from racelab.drive import ControllerInterface

class UdpInterface(ControllerInterface):
    kind = "udp"

    def open(self):                 # raise to report the device unreachable: retried every 2 s
        ...
    def send(self, values, lanes):  # values: 6 x (None | 0..15), lanes: the slots whose button is held
        ...
    def poll(self):                 # read the device; call self.feedback({"key": "value"}) per report
        ...
    def close(self):
        ...
```

Any exception from these methods means the device is lost: the bots are held at 0, and the
device is closed, then opened again. `racelab/drive/serial.py` is the complete serial
implementation, under 100 lines. To make a new kind selectable, build it in
`RaceLabApp._build_interface`.
