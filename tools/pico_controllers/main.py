# Pico = the 6 hand controllers of a Carrera Digital 124/132 Control Unit, driven over a
# Bluetooth serial module (HC-05, classic Bluetooth SPP). MicroPython, copy as main.py on the Pico.
# Wiring, socket and setup: README.md next to this file.
#
# Bluetooth side: the CU's own `C` frame (flatout docs/bot-protocol.md), one per line:
#     C <t0><l0> ... <t5><l5> <checksum>      optional leading `"`, ends with `$` or newline
#   t: `0` = slot not driven (its real controller drives), `P` = throttle 0,
#      `1`-`9` `:` `;` `<` `=` `>` `?` = throttle 1-15;  l: `1` = lane-change button held.
#   No valid frame for 750 ms: every slot goes back to its real controller (same rule as the CU).
#   Back to the computer, once a second: one status line, `pico sync=...` (see status()).
#
# CU side: the controller pulse line the CU decodes into its per-address table (firmware V339:
# INT0/PD2 or INT2/PB2, ISRs at 0x091C/0x09CC, decoder 0x0A8E). One word per controller:
# start (low 500 us, then high), then 8 Manchester bits, 500 us each, the level after the
# mid-bit edge is the bit: a1 a0 G3 G2 G1 G0 SW a2 (address a, throttle G, SW 1 = lane change).
# The CU uses each slot's latest word once per rail cycle (75.1 ms), then forgets it.
#
# Sync (SYNC = True): like a WIRELESS+ receiver, the Pico listens to the CU's sync burst (contact 6,
# through a divider to GP26) and sends each driven slot once per cycle, in that address's time slot.
# It never talks during the burst (the CU is deaf then) nor over another receiver's slots, so a
# real receiver on the other socket keeps its controllers. No burst seen: the Pico stays silent.
try:
    import gc
    import rp2
    import _thread
    from machine import ADC, Pin, UART
    from time import ticks_ms, ticks_us, ticks_diff, ticks_add
except ImportError:  # CPython: test_pulse.py only uses the pure functions
    rp2 = None

SYM_US = 125       # one PIO symbol (a quarter bit); the V339 accepts a bit edge 392-776 us after the previous one
START_US = 500     # start: line low that long
FIRST_US = 1000    # rise -> first mid-bit edge, as real receivers send it (slotbaer: high 750 us, or 1000 us
                   # when the first bit's first half is high). Do not move it for margin: 1125 us, mid-window
                   # for the hardware-5 decoder, made a hardware-8 CU misread the words half a bit late
FRAME = 48         # symbols per word (6 ms): start 4 + 6 + data 32 + idle 6 (the CU closes a word 776 us after
                   # its last edge). Sent as two 24-bit FIFO entries.
DATA_PIN = 15      # GPIO -> 1k -> NPN base; collector on the CU's pulse line (pulled up by the CU)
DIRECT = False     # True: no transistor, GPIO -> 1k -> the line, driven open-drain. Only if the line idles at
                   # 3.3 V or less, or with a Pico 2 (RP2350, 5 V tolerant GPIO); never a Pico 1 on a 5 V line
UART_ID, TX_PIN, RX_PIN = 0, 0, 1  # module TX -> GP1, module RX <- GP0 (status lines)
# the module's UART rate is whatever its AT setup says: bytes arriving with no valid frame for
# BAUD_SWITCH_MS = wrong rate, try the next one (HC-05 default first)
BAUDS = (9600, 115200, 38400, 57600)
BAUD_SWITCH_MS = 2000
TIMEOUT_MS = 750

SYNC = True        # False: no sync wire, send the slots in turn nonstop (collides with other receivers and with
                   # the CU's burst; a hardware-8 CU then misreads some words)
SYNC_PIN = 26      # ADC: contact 6 -> 5.4k -> GP26 -> 1k -> GND (the burst reaches the CU's supply, ~14-19 V)
SYNC_DIV = 6.4     # (5.4k + 1k) / 1k, for the status line only
SYNC_HI_V = 0.30   # on GP26: a burst above, quiet below SYNC_LO_V (0.3 V = ~1.9 V on contact 6)
SYNC_LO_V = 0.15
SYNC_GAP_US = 20000  # a rise after this long without one starts a burst (the burst rises twice, 1.6 ms apart)
SYNC_LOST_MS = 300   # no burst for that long: reported as lost (words only ever follow a burst)
# time slots: the CU cycle is 10 rail words of WORD_US; after the burst, the controller words leave in
# the order 6 7 9 [0] [4] [1] [5] [2] 8 [3] (positions 3 4 5 6 7 9), and a receiver sends each word
# LEAD_US before (slotbaer); the CU consumes it when it builds that rail word
WORD_US = 7510
LEAD_US = 13000
SLOT_POS = (3, 5, 7, 9, 4, 6)                     # rail position of addresses 0..5
SLOT_US = tuple(p * WORD_US - LEAD_US for p in SLOT_POS)
PRESENCE = True    # like a receiver, pull the line low 250 us right after the burst starts ("I am here")


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
    """Line levels (1 = released/high) of one controller word, one per SYM_US."""
    bits = (addr >> 1 & 1, addr & 1, thr >> 3 & 1, thr >> 2 & 1, thr >> 1 & 1, thr & 1, lane, addr >> 2 & 1)
    out = [0] * (START_US // SYM_US) + [1] * (FIRST_US // SYM_US - 2)  # start, rise, high until the first cell
    for b in bits:
        out += [1 - b, 1 - b, b, b]
    return out + [1] * (FRAME - len(out))


def symbols(addr, thr, lane):
    """The word for the PIO, first symbol in the top bit; 1 = NPN on = line pulled low."""
    word = 0
    for lv in levels(addr, thr, lane):
        word = word << 1 | (1 - lv)
    return word


def schedule(slots):
    """This cycle's words: (us after the burst starts, address), each driven slot in its own slot."""
    return sorted((SLOT_US[a], a) for a in range(6) if slots[a])


if rp2:  # one symbol per SM clock; empty FIFO = stall on the last (released) level
    @rp2.asm_pio(out_init=rp2.PIO.OUT_LOW, out_shiftdir=rp2.PIO.SHIFT_LEFT,
                 autopull=True, pull_thresh=24)
    def _npn():
        out(pins, 1)     # 1 = pin high = NPN on = line low

    @rp2.asm_pio(out_init=rp2.PIO.IN_LOW, out_shiftdir=rp2.PIO.SHIFT_LEFT,
                 autopull=True, pull_thresh=24)
    def _direct():
        out(pindirs, 1)  # 1 = pin drives its 0 = line low; 0 = pin floats, the CU's pull-up lifts the line

    # burst detector, alone on the second core: [count, start of the last burst (ticks_us),
    # sum and count of the periods, peak ADC] since the last status line. No allocation in the loop.
    _sync = [0, 0, 0, 0, 0]

    def _sync_loop():
        adc = ADC(Pin(SYNC_PIN))
        hi_th, lo_th = int(SYNC_HI_V * 65535 / 3.3), int(SYNC_LO_V * 65535 / 3.3)
        high, last_rise = False, ticks_us()
        s = _sync
        while True:
            v = adc.read_u16()
            if v > s[4]:
                s[4] = v
            if high:
                if v < lo_th:
                    high = False
            elif v > hi_th:
                high = True
                now = ticks_us()
                gap = ticks_diff(now, last_rise)
                last_rise = now
                if gap > SYNC_GAP_US:
                    if s[0] and gap < 200000:
                        s[2] += gap
                        s[3] += 1
                    s[1] = now
                    s[0] += 1


def main():
    if DIRECT:
        Pin(DATA_PIN, Pin.IN, None)  # no pad pull-down: the CU's pull-up alone must make the high level
    sm = rp2.StateMachine(0, _direct if DIRECT else _npn, freq=1_000_000 // SYM_US, out_base=Pin(DATA_PIN))
    sm.active(1)
    if SYNC:
        _thread.start_new_thread(_sync_loop, ())
    rate = 0
    uart = UART(UART_ID, BAUDS[rate], tx=Pin(TX_PIN), rx=Pin(RX_PIN), rxbuf=512)
    slots, buf, last, nxt, good = [None] * 6, b"", ticks_ms(), 0, ticks_ms()
    seen, todo, frames, words, report = 0, [], 0, 0, ticks_ms()
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
                    frames += 1
            if ticks_diff(ticks_ms(), good) > BAUD_SWITCH_MS:  # bytes, nothing readable: wrong rate
                rate = (rate + 1) % len(BAUDS)
                uart = UART(UART_ID, BAUDS[rate], tx=Pin(TX_PIN), rx=Pin(RX_PIN), rxbuf=512)
                buf, good = b"", ticks_ms()
        if ticks_diff(ticks_ms(), last) > TIMEOUT_MS:
            slots = [None] * 6
        if SYNC:
            count, start = _sync[0], _sync[1]
            if count != seen:                     # a new burst: this cycle's words, in their slots
                seen = count
                if PRESENCE and any(slots):
                    sm.put(0b11 << 22, 8)         # 250 us low (2 symbols) during the burst: the CU only counts it
                todo = [(ticks_add(start, us), a) for us, a in schedule(slots)]
                gc.collect()                      # now, 9 ms before the first slot, never in the middle of one
            if todo and ticks_diff(ticks_us(), todo[0][0]) >= 0:
                due, a = todo.pop(0)
                if slots[a] and ticks_diff(ticks_ms(), last) <= TIMEOUT_MS \
                        and ticks_diff(ticks_us(), due) < 1000:   # late by more than 1 ms: skip, never spill over
                    w = symbols(a, *slots[a])
                    sm.put(w >> 24, 8)            # put() left-aligns each 24-bit half
                    sm.put(w & 0xFFFFFF, 8)
                    words += 1
        elif sm.tx_fifo() == 0:  # nonstop: one word queued at most, a new frame reaches the line within ~12 ms
            for k in range(6):
                a = (nxt + k) % 6
                if slots[a]:
                    w = symbols(a, *slots[a])
                    sm.put(w >> 24, 8)
                    sm.put(w & 0xFFFFFF, 8)
                    words += 1
                    nxt = a + 1
                    break
        if ticks_diff(ticks_ms(), report) >= 1000:
            report = ticks_ms()
            uart.write(status(frames, words, BAUDS[rate], slots))
            frames = words = 0


def status(frames, words, baud, slots):
    """One line a second back over Bluetooth: what the Pico sees of the CU and of the computer."""
    s = _sync
    n, psum, pn, peak = s[0], s[2], s[3], s[4]
    s[2] = s[3] = s[4] = 0
    lost = ticks_diff(ticks_us(), s[1]) > SYNC_LOST_MS * 1000 or not n
    period = "%.2fms" % (psum / pn / 1000) if pn else "-"
    peak_v = peak * 3.3 / 65535 * SYNC_DIV
    driven = "".join("-" if v is None else "%X" % v[0] for v in slots)
    return "pico sync=%s period=%s peak=%.1fV bursts=%d frames=%d/s words=%d/s baud=%d slots=%s\n" % (
        "lost" if SYNC and lost else ("on" if SYNC else "off"), period, peak_v, n, frames, words, baud, driven)


if rp2:
    main()
