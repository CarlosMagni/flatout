"""Configuration: TOML + UI settings (settings.json)."""

from __future__ import annotations

import pytest

from racelab.__main__ import build_config, parse_args
from racelab.config import ConfigError, load_config, save_overrides


def test_overrides_roundtrip_in_custom_data_dir(tmp_path):
    cfg = load_config(overrides=False, data_dir=str(tmp_path))
    cfg.drivers[0].name = "Carlos"
    cfg.bots = {"0": {"kind": "target", "target_ms": 6500}}
    save_overrides(cfg)
    # --data-dir must read the settings back where they were written
    again = build_config(parse_args(["--sim", "--data-dir", str(tmp_path)]))
    assert again.drivers[0].name == "Carlos"
    assert again.bots["0"]["target_ms"] == 6500


def test_toml_validation(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[safety]\nmax_throttle = 20\n')
    with pytest.raises(ConfigError):
        load_config(str(bad), overrides=False)
    typo = tmp_path / "typo.toml"
    typo.write_text('[app]\nmdoe = "classic"\n')
    with pytest.raises(ConfigError):
        load_config(str(typo), overrides=False)
    ok = tmp_path / "ok.toml"
    ok.write_text('[app]\nmode = "classic"\n[sim]\nhumans = [0]\n[[drivers]]\nslot = 2\nname = "Julie"\n')
    cfg = load_config(str(ok), overrides=False)
    assert cfg.app.mode == "classic" and cfg.sim.humans == [0] and cfg.drivers[2].name == "Julie"
