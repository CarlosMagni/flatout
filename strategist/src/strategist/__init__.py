"""Strategy v2 — the RL strategist for the RaceLab race brain.

The pilot keeps the throttle; the strategist picks among the brain's legal moves a few times
per lap (lane at the next changer, pit at the next entry, the MPC's `risk`) and learns from
race results. It imports `racelab`; RaceLab never imports or names it. See README.md.
"""

__all__ = ["observation", "env"]
