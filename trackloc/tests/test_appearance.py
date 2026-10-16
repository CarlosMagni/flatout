"""Colour signatures of cars: same car = close, different cars = far, too few pixels = no signature."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

from trackloc.appearance import SIG_BINS, blend, distance, signature  # noqa: E402


def patch(bgr, size=20, brightness=1.0):
    img = np.full((80, 80, 3), (120, 130, 140), np.uint8)  # the table
    mask = np.zeros((80, 80), np.uint8)
    img[30:30 + size, 30:30 + size] = np.clip(np.array(bgr) * brightness, 0, 255)
    mask[30:30 + size, 30:30 + size] = 255
    return img, mask, (28, 28, 30 + size + 2, 30 + size + 2)


def sig(bgr, **kw):
    img, mask, box = patch(bgr, **kw)
    return signature(img, mask, box)


def test_a_signature_is_a_distribution_and_tells_cars_apart():
    red, blue, yellow = sig((40, 40, 215)), sig((215, 120, 30)), sig((30, 205, 225))
    assert len(red) == SIG_BINS and sum(red) == pytest.approx(1.0)
    assert distance(red, red) == pytest.approx(0.0, abs=1e-6)
    assert min(distance(red, blue), distance(red, yellow), distance(blue, yellow)) > 0.6


def test_the_same_car_in_other_light_stays_close_and_grey_cars_have_a_signature_too():
    red = sig((40, 40, 215))
    assert distance(red, sig((40, 40, 215), brightness=0.8)) < 0.3
    white, black = sig((225, 225, 225)), sig((15, 15, 15))
    assert distance(white, black) > 0.6 and distance(white, sig((40, 40, 215))) > 0.6  # no hue, told apart by brightness


def test_a_mask_at_another_resolution_and_too_few_pixels():
    img, mask, box = patch((40, 40, 215), size=24)
    half = mask[::2, ::2]  # the detector works at a reduced width
    assert distance(signature(img, half, box, 0.5), signature(img, mask, box)) < 0.1
    assert signature(*patch((40, 40, 215), size=4)) is None  # a glimpse of a car says nothing


def test_blend_moves_slowly_towards_the_new_colours():
    a, b = sig((40, 40, 215)), sig((215, 120, 30))
    assert blend(None, a) == a and distance(blend(a, b, 0.1), a) < distance(blend(a, b, 0.1), b)
