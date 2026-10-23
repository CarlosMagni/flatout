"""Check main.py against the CU: python3 controller_interfaces/pico/test_pulse.py [--emu | --capture FILE.csv]

Without --emu: frame parser, then the controller words through a Python port of the V339 pulse
ISRs (INT0 0x091C, TIMER0_COMP 0x081A, decoder 0x0A8E; ATmega16 at 8 MHz, Timer0 clk/64 = 8 us),
with the CU's 2.1 ms listening pause (ctl_window_start, once per rail cycle) dropped at every
offset: a word cut by the pause may be lost, never decoded wrong, and every slot still gets a
word in every rail cycle (otherwise the CU falls back to the jack: throttle 0 for 75 ms).
With --capture FILE.csv: decode a logic-analyzer capture of the Pico's GP15 (first bench step).
With --emu: the same words on PD2 of the stock V339 image in the simavr emulator (firmware/emu/, a local
prototype not in the repository yet), read back on the rails; skipped without it.
"""
import sys
from pathlib import Path

from main import DIRECT, FRAME, SLOT_POS, SLOT_US, SYM_US, WORD_US, levels, parse, schedule

ROOT = Path(__file__).resolve().parents[2]

T = 8           # Timer0 tick, us
WINDOW = 2100   # 160 compares of 105 cycles at 8 MHz
CYCLE = 75100   # one rail cycle (measured in the emulator), one pause


def edges(words):
    """Level changes (t_us, level) of the controller words sent back to back."""
    out, prev, t = [], 1, 0
    for w in words:
        for lv in levels(*w):
            if lv != prev:
                out.append((t, lv))
                prev = lv
            t += SYM_US
    return out


def cu_decode(edge_list, pauses=()):
    """[(publish time, (addr, throttle, lane))] as the V339 stores them in its table at 0x0180."""
    out, phase, shift, ref, ocr = [], 0, 0, 0, None
    events = sorted([(t, 1, lv) for t, lv in edge_list] + [(p, 0, None) for p in pauses])
    paused_until = -1

    def timeout_before(t):
        nonlocal phase, shift, ocr
        if ocr is not None and ref + ocr * T <= t:
            n, w, end = phase, shift, ref + ocr * T
            phase, ocr = 0, None
            if n in (9, 10):        # ctl_pulse_decode
                b = (w & 1) * 4 if n == 10 else 0
                w = w if n == 10 else (w << 1) & 0xFF
                v = (w << 3) & 0x10 | (w >> 2) & 0x0F
                out.append((end, ((w >> 6) & 3 | b, v & 15, v >> 4)))

    for t, kind, lv in events:
        if kind == 0:               # ctl_window_start: INT off, Timer0 taken; phase 0 at the end
            timeout_before(t)
            ocr, paused_until = None, t + WINDOW
            phase = 0
            continue
        if t < paused_until:
            continue
        timeout_before(t)
        dt = (t - ref) // T
        if phase == 0:
            if lv == 0:
                ref, ocr, phase = t, 0x7D, 1
        elif phase == 1:
            if lv == 1:
                ref, ocr, phase = t, 0x9D, 2
        elif dt >= (0x72 if phase == 2 else 0x31):
            ref, ocr, phase = t, 0x61, phase + 1
            shift = (shift << 1 | lv) & 0xFF
    timeout_before(float("inf"))
    return out


def check_model():
    assert parse(b'"C008150<0P0?09$') == [None, (8, True), (5, False), (12, False), (0, False), (15, False)]
    assert parse(b"C0080000000008\r\n") == [None, (8, False)] + [None] * 4
    assert parse(b"C0080000000007") is None          # bad checksum
    assert parse(b"C0X80000000008") is None          # bad throttle char

    words = [(a, g, lane) for g in range(16) for lane in (0, 1) for a in range(6)]
    frame_us = FRAME * SYM_US
    stream = edges(words)
    got = cu_decode(stream)
    assert [w for _, w in got] == words, got[:8]

    for start in range(0, CYCLE, 50):
        got = cu_decode(stream, range(start, len(words) * frame_us, CYCLE))
        for t, w in got:
            assert w == words[int(t // frame_us)], (start, t, w)   # published during its own frame
        for a in range(6):
            ts = [0] + [t for t, w in got if w[0] == a] + [len(words) * frame_us]
            assert max(b - a for a, b in zip(ts, ts[1:])) < CYCLE, (start, a)
    # the CU's 100 nF on the line (1 kOhm pull-up) delays the rising edges: words still decode
    for slow_rise in (40, 80):
        late = [(t + slow_rise if lv else t, lv) for t, lv in stream]
        assert [w for _, w in cu_decode(late)] == words, slow_rise
    print("model: 192 words decoded; CU pause at every 50 us offset: no wrong word, no slot misses a cycle;"
          " rising edges up to 80 us late still decode")


def check_sync():
    """Synced mode: each driven slot once per cycle in its time slot, the presence pulse in the burst."""
    assert WORD_US * 10 == CYCLE
    word_us, rounds = FRAME * SYM_US, 6
    for k in range(4):                            # slots overlap neither each other, nor the burst
        spans = sorted((us, us + word_us) for us, _ in schedule([(k, False)] * 6))
        assert spans[0][0] >= WINDOW and spans[-1][1] <= CYCLE
        assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:])), spans
    out, prev, sent = [], 1, []
    for r in range(rounds):
        burst = r * CYCLE
        for t, lv in ((burst + 60, 0), (burst + 310, 1)):          # presence pulse, 250 us
            out.append((t, lv))
        for us, a in schedule([(0, False)] * 6):
            word = (a, (a + 3 * r) % 16, r & 1)
            sent.append((burst, word))
            t = burst + us
            for lv in levels(*word):
                if lv != prev:
                    out.append((t, lv))
                    prev = lv
                t += SYM_US
    got = cu_decode(out, range(0, rounds * CYCLE, CYCLE))
    assert [w for _, w in got] == [w for _, w in sent], got[:6]
    for (t, (a, g, lane)), (burst, _) in zip(got, sent):
        assert t < burst + SLOT_POS[a] * WORD_US, (a, t - burst)        # decoded before the CU uses it
    print("sync: %d rounds, every slot decoded once per cycle in its time slot, before the CU uses it,"
          " none during the burst" % rounds)


def check_emu():
    sys.path[:0] = [str(ROOT / "firmware" / "emu"), str(ROOT / "carreralib" / "src")]
    try:
        from emu import Emulator, unavailable
    except ImportError as e:
        print("emulator: skipped (%s)" % e)
        return
    reason = unavailable()
    if reason:
        print("emulator: skipped (%s)" % reason)
        return

    want = [(0, 3, 0), (1, 7, 1), (2, 10, 0), (3, 1, 0), (4, 9, 1), (5, 6, 0)]
    with Emulator() as emu:
        sent, prev = emu.t, 1
        for _ in range(42):                          # 42 rounds of 36 ms, 20 rail cycles
            for w in want:
                for lv in levels(*w):
                    if lv != prev:
                        emu.proc.stdin.write("pin D2 %d\n" % lv)
                        prev = lv
                    emu.run(SYM_US / 1000)
        for a, g, lane in want:
            seen = {(w["thr"], w["sw"]) for w in emu.slot_words(a, since=sent + 80)}
            assert seen == {(g, 1 - lane)}, (a, seen)
    print("emulator: stock V339 sends the 6 slots' throttle and lane from the PD2 pulse words")


def check_capture(path):
    """Logic-analyzer CSV of GP15 (time in s, level) through the CU model. NPN: GP15 high = line low;
    DIRECT: GP15 is the line (on the bench, without the CU, add a 10k pull-up to 3.3 V)."""
    out, prev = [], 1
    for row in open(path):
        try:
            t, v = (float(x) for x in row.split(",")[:2])
        except ValueError:
            continue                                # header
        lv = int(v) if DIRECT else 1 - int(v)
        if lv != prev:
            prev = lv
            out.append((round(t * 1e6), prev))
    for t, (a, g, lane) in cu_decode(out):
        print("%9.1f ms  slot %d  throttle %2d  lane %d" % (t / 1000, a, g, lane))


if __name__ == "__main__":
    if "--capture" in sys.argv:
        check_capture(sys.argv[sys.argv.index("--capture") + 1])
        sys.exit()
    check_model()
    check_sync()
    if "--emu" in sys.argv:
        check_emu()
