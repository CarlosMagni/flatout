"""RaceLab Python client: `RaceLabClient` (asyncio), `SyncClient` (blocking) and
`RaceLabEnv` (Gymnasium interface). See docs/api.md and examples/README.md."""

from .client import DEFAULT_URL, Event, RaceLabClient, RaceLabError, SyncClient
from .env import OBS_FIELDS, POS_OBS_FIELDS, Frame, RaceLabEnv, default_obs, default_reward

__all__ = [
    "DEFAULT_URL",
    "OBS_FIELDS",
    "POS_OBS_FIELDS",
    "Event",
    "Frame",
    "RaceLabClient",
    "RaceLabEnv",
    "RaceLabError",
    "SyncClient",
    "default_obs",
    "default_reward",
]
