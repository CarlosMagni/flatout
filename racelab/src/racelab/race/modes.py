"""Session types and settings (`SessionSettings`)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

SESSION_TYPES: dict[str, dict[str, Any]] = {
    "practice": {
        "label": "Practice",
        "description": "Free running, ranked by best lap, optional duration.",
        "advanced_only": False,
        "uses_lights": False,
        "params": ["duration_s"],
    },
    "qualifying": {
        "label": "Qualifying",
        "description": "Time-limited, ranked by best lap; the lap in progress is completed.",
        "advanced_only": False,
        "uses_lights": False,
        "params": ["duration_s"],
    },
    "race": {
        "label": "Race",
        "description": "Lights start; first to complete the set number of laps wins.",
        "advanced_only": False,
        "uses_lights": True,
        "params": ["laps", "fuel"],
    },
    "endurance": {
        "label": "Endurance",
        "description": "Fixed-duration race; most laps wins.",
        "advanced_only": False,
        "uses_lights": True,
        "params": ["duration_s", "fuel"],
    },
    "elimination": {
        "label": "Elimination",
        "description": "The last car is eliminated every N laps of the leader; the last one on track wins.",
        "advanced_only": False,
        "uses_lights": True,
        "params": ["elim_every", "fuel"],
    },
    "rival": {
        "label": "Beat the bot",
        "description": "Race against a car driven by the software.",
        "advanced_only": True,
        "uses_lights": True,
        "params": ["laps", "rival_slot", "rival_margin_pct", "fuel"],
    },
    "training": {
        "label": "AI training",
        "description": "Continuous running split into episodes of a few laps, for agents.",
        "advanced_only": True,
        "uses_lights": False,
        "params": ["episode_laps"],
    },
}

BEST_LAP_TYPES = frozenset({"practice", "qualifying", "training"})  # ranked by best lap
LAP_TARGET_TYPES = frozenset({"race", "rival"})  # finish at the lap count
TIMED_TYPES = frozenset({"practice", "qualifying", "endurance"})  # finish when the duration expires
DURATION_REQUIRED_TYPES = frozenset({"qualifying", "endurance"})


def format_duration(seconds: float) -> str:
    """90 -> '1 min 30 s', 900 -> '15 min', 5400 -> '1 h 30 min'."""
    hours, rest = divmod(round(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{hours} h" if hours else "", f"{minutes} min" if minutes else "", f"{secs} s" if secs else ""]
    return " ".join(p for p in parts if p) or "0 s"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_int(label: str, value: Any, low: int, high: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{label}: must be an integer between {low} and {high}")


@dataclass
class SessionSettings:
    type: str = "practice"
    laps: int = 20
    duration_s: float | None = None
    elim_every: int = 3
    episode_laps: int = 5
    rival_slot: int | None = None
    rival_margin_pct: float = 3.0
    fuel: bool = True
    title: str = ""

    def validate(self) -> None:
        """Raise ValueError if a setting is out of range."""
        if self.type not in SESSION_TYPES:
            raise ValueError(f"Unknown session type: {self.type!r}")
        _check_int("Number of laps", self.laps, 1, 9999)
        _check_int("Eliminate every N laps", self.elim_every, 1, 999)
        _check_int("Laps per episode", self.episode_laps, 1, 999)
        if self.duration_s is not None and not (_is_number(self.duration_s) and 0 < self.duration_s <= 86400):
            raise ValueError("Duration: must be a number of seconds between 0 and 86400")
        if self.duration_s is None and self.type in DURATION_REQUIRED_TYPES:
            raise ValueError("Duration is required for this session type")
        if self.rival_slot is not None:
            _check_int("Rival car", self.rival_slot, 0, 7)
        # negative margin = the bot aims faster than the rival
        if not (_is_number(self.rival_margin_pct) and -20 <= self.rival_margin_pct <= 50):
            raise ValueError("Rival margin: must be a percentage between -20 and 50")
        if not isinstance(self.fuel, bool):
            raise ValueError("Fuel: must be true or false")
        if not isinstance(self.title, str) or len(self.title) > 80:
            raise ValueError("Title: must be text of at most 80 characters")

    def default_title(self) -> str:
        """Title derived from the type and settings, e.g. 'Race · 20 laps'."""
        label = SESSION_TYPES[self.type]["label"]
        detail = ""
        if self.type in LAP_TARGET_TYPES:
            detail = f"{self.laps} lap{'s' if self.laps > 1 else ''}"
        elif self.type in TIMED_TYPES and self.duration_s is not None:
            detail = format_duration(self.duration_s)
        elif self.type == "elimination":
            detail = "every lap" if self.elim_every == 1 else f"every {self.elim_every} laps"
        elif self.type == "training":
            detail = f"{self.episode_laps}-lap episodes"
        return f"{label} · {detail}" if detail else label

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionSettings:
        """Build and validate settings; unknown keys are ignored."""
        if not isinstance(data, dict):
            raise ValueError("Session settings: object expected")
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        for key in ("laps", "elim_every", "episode_laps", "rival_slot"):
            if isinstance(values.get(key), float) and values[key].is_integer():
                values[key] = int(values[key])  # JSON : 20.0 -> 20
        settings = cls(**values)
        settings.validate()
        return settings
