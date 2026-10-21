# Pico = the 6 hand controllers of a Carrera Digital 124/132 Control Unit, driven over a
# Bluetooth serial module (HC-05, classic Bluetooth SPP). MicroPython, copy as main.py on the Pico.
# Wiring, socket and setup: README.md next to this file.
#
# Bluetooth side: the CU's own `C` frame (flatout docs/bot-protocol.md), one per line:
#     C <t0><l0> ... <t5><l5> <checksum>      optional leading `"`, ends with `$` or newline
#   t: `0` = slot not driven (its real controller drives), `P` = throttle 0,
#      `1`-`9` `:` `;` `<` `=` `>` `?` = throttle 1-15;  l: `1` = lane-change button held.
#   No valid frame for 750 ms: every slot goes back to its real controller (same rule as the CU).
#
# CU side: the controller pulse line the CU decodes into its per-address table (firmware V339:
# INT0/PD2 or INT2/PB2, ISRs at 0x091C/0x09CC, decoder 0x0A8E). One word per controller:
# start (low 1 bit, high 2 bits), then 8 Manchester bits, 500 us each, the level after the
# mid-bit edge is the bit: a1 a0 G3 G2 G1 G0 SW a2 (address a, throttle G, SW 1 = lane change).
# The CU uses each slot's latest word once per rail cycle (75.1 ms), then forgets it.
try:
    import rp2
    from machine import Pin, UART
    from time import ticks_ms, ticks_diff
except ImportError:  # CPython: test_pulse.py only uses the pure functions
    rp2 = None

HALF_US = 250      # half a bit; the V339 accepts a bit edge 392-776 us after the previous one
FRAME = 24         # half-bits per word: start 5 + data 16 + idle 3 (the CU closes a word 776 us after its last
                   # edge). 6 words = 36 ms: every slot lands twice per 75.1 ms rail cycle, and the CU's 2.1 ms
                   # pause per cycle can only eat one of them.
DATA_PIN = 15      # GPIO -> 1k -> NPN base; collector on the CU's pulse line (pulled up by the CU)
DIRECT = False     # True: no transistor, GPIO -> 1k -> the line, driven open-drain. Only if the line idles at
                   # 3.3 V or less, or with a Pico 2 (RP2350, 5 V tolerant GPIO); never a Pico 1 on a 5 V line
UART_ID, TX_PIN, RX_PIN = 0, 0, 1  # module TX -> GP1
# the module's UART rate is whatever its AT setup says: bytes arriving with no valid frame for
# BAUD_SWITCH_MS = wrong rate, try the next one (HC-05 default first)
BAUDS = (9600, 115200, 38400, 57600)
BAUD_SWITCH_MS = 2000
TIMEOUT_MS = 750


def parse(frame):
    """b'"C008150<0P0?09$' -> [(throttle 0-15, lane) or None] * 6, or None if invalid."""
    f = frame.strip().lstrip(b'"').rstrip(b"$")
    if len(f) != 14 or f[0] != ord("C") or (sum(f[1:13]) & 15) + 48 != f[13]:
        return None
    slots = []
    for i in range(1, 13, 2):
        t, l = f[i], f[i + 1]
        if t == ord("0"):
            slots.append(None)
        elif t == ord("P") or 0x31 <= t <= 0x3F:
            slots.append((0 if t == ord("P") else t - 48, l == ord("1")))
        else:
            return None
    return slots


def levels(addr, thr, lane):
    """Line levels (1 = released/high) of one controller word, one per half-bit."""
    bits = (addr >> 1 & 1, addr & 1, thr >> 3 & 1, thr >> 2 & 1, thr >> 1 & 1, thr & 1, lane, addr >> 2 & 1)
    out = [0, 0, 1, 1, 1]  # start: low, rise, the first mid-bit edge comes 1000 us after the rise
    for b in bits:
        out += [1 - b, b]
    return out + [1] * (FRAME - len(out))


def symbols(addr, thr, lane):
    """The word for the PIO, first half-bit in the top bit; 1 = NPN on = line pulled low."""
    word = 0
    for lv in levels(addr, thr, lane):
        word = word << 1 | (1 - lv)
    return word


if rp2:  # one half-bit per SM clock; empty FIFO = stall on the last (released) level
    @rp2.asm_pio(out_init=rp2.PIO.OUT_LOW, out_shiftdir=rp2.PIO.SHIFT_LEFT,
                 autopull=True, pull_thresh=FRAME)
    def _npn():
        out(pins, 1)     # 1 = pin high = NPN on = line low

    @rp2.asm_pio(out_init=rp2.PIO.IN_LOW, out_shiftdir=rp2.PIO.SHIFT_LEFT,
                 autopull=True, pull_thresh=FRAME)
    def _direct():
        out(pindirs, 1)  # 1 = pin drives its 0 = line low; 0 = pin floats, the CU's pull-up lifts the line


def main():
    if DIRECT:
        Pin(DATA_PIN, Pin.IN, None)  # no pad pull-down: the CU's pull-up alone must make the high level
    sm = rp2.StateMachine(0, _direct if DIRECT else _npn, freq=1_000_000 // HALF_US, out_base=Pin(DATA_PIN))
    sm.active(1)
    rate = 0
    uart = UART(UART_ID, BAUDS[rate], tx=Pin(TX_PIN), rx=Pin(RX_PIN), rxbuf=512)
    slots, buf, last, nxt, good = [None] * 6, b"", ticks_ms(), 0, ticks_ms()
    while True:
        data = uart.read()
        if data:
            parts = (buf + data).replace(b"\n", b"$").split(b"$")
            buf = parts.pop()[-64:]
            for p in parts:
                s = parse(p)
                if s:
                    slots, last = s, ticks_ms()
                    good = last
            if ticks_diff(ticks_ms(), good) > BAUD_SWITCH_MS:  # bytes, nothing readable: wrong rate
                rate = (rate + 1) % len(BAUDS)
                uart = UART(UART_ID, BAUDS[rate], tx=Pin(TX_PIN), rx=Pin(RX_PIN), rxbuf=512)
                buf, good = b"", ticks_ms()
        if ticks_diff(ticks_ms(), last) > TIMEOUT_MS:
            slots = [None] * 6
        if sm.tx_fifo() == 0:  # one word queued at most: a new frame reaches the line within ~12 ms
            for k in range(6):
                a = (nxt + k) % 6
                if slots[a]:
                    sm.put(symbols(a, *slots[a]), 32 - FRAME)  # put() left-aligns it
                    nxt = a + 1
                    break


if rp2:
    main()
