# Pico: a controller interface for socket 1

The reference [controller interface](../../docs/controller-interface.md). A Raspberry Pi Pico
(MicroPython) on the CU's **controller socket 1** plays the 6 hand controllers like a WIRELESS+
receiver, and takes its orders from RaceLab over Bluetooth (an HC-05 serial module). The CU
only sees controllers: start lights, false starts and fuel apply as usual, and no CU firmware
is involved. These are the bots for CUs without `C` or `B`.

> **Status:** works on a hardware-8 CU (v8339): RaceLab drives a car through it. The words
> were first validated against the stock V339 firmware (hardware 5) in the emulator and in a
> model of its interrupt routines.

## Parts

- Raspberry Pi Pico (RP2040) with MicroPython
- HC-05 Bluetooth serial module (classic Bluetooth SPP), any UART rate: the Pico tries 9600,
  115200, 38400 and 57600 until the frames read clean
- one NPN transistor (2N3904, BC547, BC337…) and a 560 Ω–1 kΩ base resistor
- for the sync: resistors making a ~1:6 divider (here 3 × 1.8 kΩ in series, and 1 kΩ)
- a 6-contact controller plug, or a sacrificed controller extension cable

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
                                    GP26 (pin 31) --+--[1.8k]-[1.8k]-[1.8k]------- contact 6 (sync, up to ~19 V)
                                                    +--[1 kΩ]-- GND (pin 33)
```

- **Data line (contact 1).** Inside the CU it has a 1 kΩ pull-up to contact 3 (~4.4 V) and
  100 nF to ground (HPI's CU schematic). The RP2040 is not 5 V tolerant, so the NPN pulls the
  line low and never drives it high (`main.py` inverts: GP15 high = line low).
  - Use **no collector resistor**: against the 1 kΩ pull-up, even 220 Ω would leave the low level
    near the CU's 1 V threshold.
  - Check the transistor's pinout with a multimeter (diode mode: base to each other leg ≈ 0.65 V).
- **Sync (contact 6).** The CU's burst follows its supply: ~14.5 V on a 14.8 V Digital 132 supply,
  17.6 V measured on a 19.5 V one. The 6.4:1 divider keeps GP26 (an ADC input) under 3.3 V up
  to ~21 V. **Never wire contact 6 to the Pico directly.**
- **Idle or crashed Pico:** the line stays released. The PIO always stalls on the released level,
  and GP15 has a pull-down at boot, so the CU falls back to its sockets.
- **Power:** over USB. A common ground with the CU is required. Never connect contact 3.
  - The HC-05 sold as "5.0" forgets its pairing when powered off.
  - Pair it again from RaceLab after a power cut.

## The socket (6 contacts)

From the HPI's measurements, its CU schematic and slotbaer's protocol pages (sources below).
Consistent with the V339 firmware, and checked on a hardware-8 CU: contact 2 rings to the
rails' ground.

| Contact | Role | Here |
|---|---|---|
| 1 | data, receiver → CU (1 kΩ pull-up to contact 3, 100 nF; low pulses) | **NPN collector** |
| 2 | ground | **Pico GND** |
| 3 | controller supply (~4.4 V) | not connected |
| 4 | lane button (pull-up) | — |
| 5 | throttle (slider wiper, 0–2.1 V) | — |
| 6 | sync, CU → receivers: a 38 kHz burst at the CU's supply voltage, 30 on / 30 off / 20 on periods, once per rail cycle | **divider → GP26** |

- A wired controller only uses contacts 2–5: on its plug, the two outer contacts are empty.
  Contact 1 is the outer one next to ground.
- In a standard flat cable (white, black, red, green, yellow, blue), contacts 1 to 6 follow the
  colours in order. Check with a meter all the same: Carrera has no colour code.
- Use **socket 1**, the WIRELESS+ receiver's. Wired controllers in sockets 3 and 4 then take
  addresses 5 and 6: the CU's manual says so, and the firmware switches on line activity.

## How it talks to the CU

**One word per controller**, about 6 ms (from the V339 firmware: INT0/INT2 ISRs 0x091C/0x09CC,
decoder 0x0A8E):
- the line goes low for 500 µs, then high;
- then 8 Manchester bits of 500 µs each. The bit is the level after the mid-bit edge;
- the order is `a1 a0 G3 G2 G1 G0 SW a2`: address 0–5, throttle 0–15, SW = 1 when the lane
  button is pressed. This is slotbaer's receiver word.
- the first mid-bit edge comes 1000 µs after the rise, as a real receiver sends it. Moving it
  for "margin" made a hardware-8 CU misread the words.

**Synced like a receiver** (`SYNC = True`):
- the second core watches the CU's burst on GP26;
- after each burst, each driven controller gets **one word, in its address's time slot**. A
  slot sits at its rail position × 7.51 ms − 13 ms after the burst, in the order 0, 4, 1, 5, 2, 3;
- it never sends during the burst (the CU is deaf then), nor over another receiver's slots: a
  WIRELESS+ receiver on socket 2 keeps its controllers;
- the CU uses each word once per rail cycle (73.5 ms on hardware 8, 75.1 ms on hardware 5);
- no burst, no word.

**Fast at slot time:** all 192 possible words are computed at boot, two FIFO halves each, and a
busy wait keeps every slot punctual. A word late by more than 1 ms is skipped and counted
(`late=`). A skipped word is a cycle without throttle, which the car feels as a brake.

## Reports and tuning (over Bluetooth)

Once a second, the Pico reports
([the protocol's report line](../../docs/controller-interface.md#reports-device--racelab-optional-recommended)).
RaceLab shows it in System → Connection:

```
pico sync=on period=73.52ms peak=17.6V bursts=1141 frames=3/s words=13/s late=0 baud=115200 slots=---0-- shift=0 presence=0 repeat=1
```

| Key | What it says |
|---|---|
| `sync` | `on` = it sees the CU's burst (no burst, no word); `lost` |
| `period` | the CU's rail cycle, measured |
| `peak` | the burst's voltage on contact 6 |
| `frames` | RaceLab frames received per second |
| `words` | words sent per second: one per cycle per driven controller (~13.6/s) |
| `late` | words skipped because they were late (should be 0) |
| `slots` | each controller's throttle in hex, `-` = not driven |

Lines starting with `#` tune it live, from a serial terminal on the module's port:
- `#shift=<us>` moves every slot;
- `#repeat=2` sends each word twice per cycle;
- `#presence=1` turns on the receiver's 250 µs "I am here" pulse. A hardware-8 CU beeps at it
  every cycle, so it is off by default.

## Setup

1. **MicroPython on the Pico:** hold BOOTSEL while plugging it in, then copy the RP2040
   MicroPython `.uf2` (micropython.org/download/RPI_PICO) onto the drive that appears.
2. **Copy the program:** `pip install mpremote`, then
   `mpremote cp controller_interfaces/pico/main.py :main.py`. Thonny works too: open `main.py`
   and save it to the Pico. It starts by itself at power-up.
3. **The HC-05:** do not press its button. Held at power-up, it enters its AT configuration mode
   (38400 baud) instead of the data mode.
   - **On macOS,** RaceLab pairs it itself at step 4, through
     [blueutil](https://github.com/toy/blueutil) (`brew install blueutil` once). macOS shows no
     *Connect* button for such a module.
   - **Elsewhere,** pair it in the system's Bluetooth settings (PIN `1234`, else `0000`).
   - **On macOS, choose the `bt:` entry** ("… (Bluetooth)"), not `/dev/cu.<name>`: RaceLab then
     opens the module's channel itself and reconnects it every time. The `/dev/cu.` port only
     worked right after pairing (a macOS bug with these modules).
   - **LED fast while RaceLab has the port:** there is no Bluetooth connection. Pair again from
     RaceLab; the old pairing is dropped first.
   - **AT mode:** the Pico can talk to the module there. `AT+NAME=<name>` renames it, and
     `AT+UART?` gives its rate (any rate works, since `main.py` finds it).
4. **RaceLab:** launch it from Terminal or RaceLab.app, which hold the Bluetooth permission.
   - Connect the CU as usual (System → Connection).
   - Under *Controller interface*: *Search ports…* → *Use* on the module's entry (`bt:…` on
     macOS). For a module not paired yet, use *Pair and use* under the Bluetooth modules in range.
   - The chip turns green, the module's LED stays on, and the report line appears.
   - Bots show *Ready · controller interface*: turn on **With bots** and use a slot's fader.
5. **First tests:** cars off the track or wheels in the air, a hand on the track's power.

## Checks

**Bench test, Pico alone (no CU):** send frames from Python, with the snippet in the
[protocol](../../docs/controller-interface.md#hardware-no-code-in-racelab).
- The report line comes back over the same port, with `sync=lost`.
- With no CU, there are no words to look at: synced mode only talks after a burst. Set
  `SYNC = False` to see words on GP15 with a logic analyzer.

**The other checks:**
- `python3 controller_interfaces/pico/test_pulse.py`: the frame parser, then the words through a
  Python port of the V339 interrupt routines.
  - nonstop mode: the CU's listening pause at every possible offset (no word misread, no
    controller left without a word for a cycle);
  - synced mode: one word per cycle in its slot, before the CU uses it, none during the burst.
- `.venv/bin/python controller_interfaces/pico/test_pulse.py --emu`: the same words on PD2 of the
  stock V339 firmware in the simavr emulator, read back on the rails. The emulator
  (`firmware/emu/`, Docker) is a local prototype, not in the repository yet: without it, the
  step is skipped.
- `… test_pulse.py --capture gp15.csv`: decodes a logic-analyzer capture of GP15 (CSV: time in s,
  level) with the CU model.
- RaceLab side: `racelab/tests/test_interface.py`.

Sources: [HPI — Handregler der Carrera-Bahn](https://osm.hpi.de/carrera/handregler) (socket,
sync signal, receiver answer), [HPI — Control Unit schematic](https://osm.hpi.de/carrera/schaltbilder/control_unit.pdf)
(sheet 5, the controller sockets), [slotbaer — D132 & D124 Infrarot-Regler-Protokoll](http://slotbaer.de/carrera-digital-124-132/13-d132-d124-infrarot-regler-protokoll.html)
(controller word, receiver timing).
