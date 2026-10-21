# Pico controller interface

A Raspberry Pi Pico (MicroPython) on the CU's **controller socket 1** plays the 6 hand
controllers. RaceLab sends it the throttles over Bluetooth (an HC-05 serial module), in the
CU's own `C` frame; the Pico turns them into the controller words the CU decodes on that
socket, the line a WIRELESS+ receiver uses. The CU sees controllers: start lights, false
starts and fuel apply as usual, and no CU firmware is involved. This is the bot path for CUs
without `C` or `B` (hardware 8, STM32). See [`P` in docs/bot-protocol.md](../../docs/bot-protocol.md#p--the-pico-controller-interface).

> **Status:** works on a hardware-8 CU (v8339): RaceLab drives a car through the Pico. The
> protocol was first validated against the stock V339 firmware (hardware 5) in the emulator
> and in a model of its interrupt routines.

## Parts

- Raspberry Pi Pico (RP2040) with MicroPython
- HC-05 Bluetooth serial module (classic Bluetooth SPP); its UART rate does not matter: the Pico
  tries 9600, 115200, 38400 and 57600 until the frames read clean
- one NPN transistor (2N3904, BC547, BC337…) and a 560 Ω–1 kΩ base resistor
- a 6-contact controller plug or a sacrificed controller extension cable

## Wiring

```
HC-05                               Pico                        CU controller socket 1
  VCC ----------------------------- VBUS (pin 40, 5 V)
  GND ----------------------------- GND (pin 38)
  TXD ----------------------------- GP1 (pin 2, UART0 RX)
  RXD ----------------------------- GP0 (pin 1, UART0 TX)
                                    GP15 (pin 20) --[1 kΩ]-- B  NPN
                                                             E -- GND (pin 18) ---- contact 2 (GND)
                                                             C ------------------- contact 1 (data to the CU)
```

- **Levels.** The HC-05's logic is 3.3 V: wired straight to the Pico. Inside the CU, contact 1
  has a 1 kΩ pull-up to contact 3 (~4.4 V) and 100 nF to ground (HPI's CU schematic); the
  RP2040 is not 5 V tolerant, so the NPN pulls the line low and never drives it high
  (`main.py` inverts: GP15 high = line low). No collector resistor: against the 1 kΩ pull-up
  even 220 Ω would leave the low level near the CU's 1 V threshold. Check the transistor's
  pinout with a multimeter (diode mode: base to each other leg ≈ 0.65 V).
- **No transistor** (`DIRECT = True` in `main.py`): GP15 → contact 1, driven open-drain. Only if
  contact 1 idles at 3.3 V or less, or with a Pico 2 (RP2350, 5 V tolerant GPIO); not on a CU
  built like the HPI schematic (4.4 V pull-up).
- **Idle or crashed Pico:** the line stays released (the PIO always stalls on the released
  level, GP15 has a pull-down at boot); the CU falls back to its sockets.
- **Power** the Pico over USB. Common ground with the CU is required. Never connect contact 3
  or contact 6.

## The socket (6 contacts)

From the HPI's measurements and CU schematic and slotbaer's protocol pages (sources below),
consistent with the V339 firmware; checked on a hardware-8 CU (contact 2 rings to the rails'
ground):

| Contact | Role | Here |
|---|---|---|
| 1 | data, receiver → CU (1 kΩ pull-up to contact 3, 100 nF; low pulses) | **NPN collector** |
| 2 | ground | **Pico GND** |
| 3 | controller supply (~4.4 V) | not connected |
| 4 | lane button (pull-up) | — |
| 5 | throttle (slider wiper, 0–2.1 V) | — |
| 6 | sync, CU → receiver: 38 kHz bursts **up to ~15 V**, 30 on / 30 off / 20 on periods every 75 ms | **never on the Pico** |

- A wired controller only uses contacts 2–5: on its plug the two outer contacts are empty.
  Contact 1 is the outer one next to ground.
- Use **socket 1** (the WIRELESS+ receiver's). Wired controllers in sockets 3 and 4 then take
  addresses 5 and 6 (the CU's manual says so; the firmware switches on line activity).
- Find the contacts before soldering: contact 2 rings to the CU's ground; contact 1 idles high
  and collapses under a 10 kΩ load to ground (a weak pull-up, not a supply); contact 6 shows
  bursts on a logic analyzer.

## Protocol (from the V339 firmware: INT0/INT2 ISRs 0x091C/0x09CC, decoder 0x0A8E)

One word per controller, about 6 ms: line low 500 µs, high, then 8 Manchester bits of 500 µs;
the bit is the level after the mid-bit edge. Order `a1 a0 G3 G2 G1 G0 SW a2` (address 0–5,
throttle 0–15, SW = 1 lane button pressed), slotbaer's receiver word. The CU keeps each slot's
latest word for one rail cycle (75.1 ms); the Pico sends the 6 slots in turn, one round every
36 ms, so the CU's 2.1 ms listening pause per cycle can never leave a slot without a word.

## Setup

1. **MicroPython on the Pico:** hold BOOTSEL while plugging it in, copy the RP2040 MicroPython
   `.uf2` (micropython.org/download/RPI_PICO) onto the drive that appears.
2. **Copy the program:** `pip install mpremote`, then
   `mpremote cp tools/pico_controllers/main.py :main.py` (or Thonny: open `main.py`, save to the Pico).
   It starts by itself at power-up.
3. **The HC-05** (do not press its button: held at power-up it enters its AT configuration
   mode, 38400 baud, instead of the data mode): RaceLab pairs it itself on macOS, step 4,
   through [blueutil](https://github.com/toy/blueutil) (`brew install blueutil` once; macOS
   shows no *Connect* button for such a module). Elsewhere, pair it in the system's Bluetooth
   settings (PIN `1234`, else `0000`).
   - LED blinking fast while RaceLab has the port open = no Bluetooth connection. A module
     whose `AT+STATE?` answers `PAIRABLE` although the computer lists it lost the pairing:
     pair it again from RaceLab (the old pairing is dropped first).
   - In AT mode (button held at power-up), the Pico can talk to the module: `AT+NAME=<name>`
     renames it, `AT+UART?` gives its rate (any rate works, `main.py` finds it).
4. **RaceLab** (launched from Terminal or RaceLab.app, which hold the Bluetooth permission):
   connect the CU as usual (System → Connection), then under *Pico controller interface*:
   *Search ports…* → *Use* on the module's port, or, for a module not paired yet, *Pair and
   use* under the Bluetooth modules in range. The chip turns green and the
   module's LED blinks slowly once connected. Bots show *Ready · Pico*: turn on **With bots**
   and use a slot's fader.
5. First tests with the cars off the track or wheels in the air, a hand on the track's power.

## Checks

**Bench test, Pico alone (no CU):** RaceLab only drives the Pico next to a real CU, so send
the frames yourself. Pair the HC-05, run this (pyserial), and watch GP15 on a logic analyzer
(or an LED + 1 kΩ to GND: it flickers while a slot is driven, goes dark ~750 ms after you stop):

```python
import serial, time

bt = serial.Serial("/dev/cu.HC-05", 9600)  # your module's port (ls /dev/cu.*)

def frame(slots):  # 6 x (throttle 0-15 or None, lane button bool)
    body = "".join(("0" if t is None else "P" if t == 0 else chr(48 + t)) + "01"[lane] for t, lane in slots)
    return ('"C' + body + chr(48 + sum(map(ord, body)) % 16) + "$").encode()

while True:  # slot 0 at throttle 8, the others not driven; resend well within 750 ms
    bt.write(frame([(8, False)] + [(None, False)] * 5))
    time.sleep(0.3)
```

- `python3 tools/pico_controllers/test_pulse.py`: the frame parser, then the words through a
  Python port of the V339 interrupt routines, with the CU's listening pause at every possible
  offset: no word decoded wrong, no slot missing a rail cycle.
- `.venv/bin/python tools/pico_controllers/test_pulse.py --emu`: the same words on PD2 of the
  stock V339 firmware in the simavr emulator (`firmware/emu`, Docker), read back on the rails.
- `… test_pulse.py --capture gp15.csv`: a logic-analyzer capture of GP15 (CSV: time in s,
  level) decoded by the CU model. First bench step, Pico alone, before the CU.
- RaceLab side: `racelab/tests/test_pico.py` (heartbeat, E-STOP, release, port loss).

## Limits

- A real WIRELESS+ receiver syncs on contact 6 and answers each sync with a 192 µs low pulse;
  the Pico sends freely and does not listen. The hardware-5 firmware does not need either
  (emulator), a hardware-8 CU might: then read contact 6 through a divider (47 kΩ / 10 kΩ).
- `HALF_US` and `FRAME` in `main.py` are the timing knobs.

Sources: [HPI — Handregler der Carrera-Bahn](https://osm.hpi.de/carrera/handregler) (socket,
sync signal, receiver answer), [HPI — Control Unit schematic](https://osm.hpi.de/carrera/schaltbilder/control_unit.pdf)
(sheet 5, the controller sockets), [slotbaer — D132 & D124 Infrarot-Regler-Protokoll](http://slotbaer.de/carrera-digital-124-132/13-d132-d124-infrarot-regler-protokoll.html)
(controller word).
