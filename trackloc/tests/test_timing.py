"""CU clock -> host clock (pure standard library)."""

from __future__ import annotations

import random

import pytest

from trackloc.timing import CU_WRAP, ClockSync


def test_offset_is_the_minimum_of_host_minus_cu_not_the_mean():
    rng = random.Random(1)
    clock = ClockSync()
    true_offset = 1000.0
    for k in range(60):
        cu_s = 5.0 + k * 1.7
        delay = rng.uniform(0.015, 0.150)  # Bluetooth polling and queuing: never negative
        clock.add(int(cu_s * 1000), true_offset + cu_s + delay)
    assert 0.0 <= clock.offset - true_offset < 0.03  # the mean would be ~80 ms late
    assert clock.cu_to_host(int(20.0 * 1000)) == pytest.approx(true_offset + 20.0, abs=0.03)


def test_unwraps_the_32_bit_cu_timestamp():
    clock = ClockSync()
    base = CU_WRAP - 3000  # three seconds before the CU clock wraps to zero
    for k in range(6):
        cu = (base + k * 1000) % CU_WRAP
        clock.add(cu, 500.0 + (base + k * 1000) / 1000.0 + 0.02)
    assert clock.offset == pytest.approx(500.02, abs=1e-6)  # no jump of 2**32 ms at the wrap
    assert clock.cu_to_host(2000) == pytest.approx(500.0 + (CU_WRAP + 2000) / 1000.0, abs=0.05)


def test_rolling_window_follows_a_drifting_crystal():
    clock = ClockSync(window_s=60.0)
    for k in range(400):  # 5 s apart, the CU clock gains 50 ppm: 20 ms over 400 s
        cu_s = k * 5.0
        clock.add(int(cu_s * 1000), 10.0 + cu_s * (1 + 50e-6) + 0.01 + (0.05 if k % 7 else 0.0))
    assert clock.offset == pytest.approx(10.0 + 400 * 5 * 50e-6 + 0.01, abs=0.006)  # tracks the drift, not the start


def test_no_estimate_before_any_event():
    clock = ClockSync()
    assert clock.offset is None and clock.cu_to_host(1234) is None
