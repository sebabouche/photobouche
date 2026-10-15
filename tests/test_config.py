from __future__ import annotations

from pathlib import Path

import pytest

from photobouche import config


def test_toml_then_state_override(tmp_path):
    toml = tmp_path / "c.toml"
    toml.write_text(
        f'[paths]\nroot = "{tmp_path}"\n[camera]\ntrigger_db = 12.0\n[show]\nlocal_clips = "~/clips"\n'
    )
    cfg = config.load(toml)
    assert cfg.camera.trigger_db == 12.0
    assert cfg.show.local_clips == Path("~/clips").expanduser()
    cfg.camera.trigger_db = 18.0
    cfg.booth_screen.mode = "latest"
    cfg.save_state()
    again = config.load(toml)
    assert again.camera.trigger_db == 18.0
    assert again.booth_screen.mode == "latest"


def test_unknown_key_is_an_error(tmp_path):
    toml = tmp_path / "c.toml"
    toml.write_text("[camera]\ntriger_db = 12\n")
    with pytest.raises(ValueError, match="triger_db"):
        config.load(toml, root=tmp_path)
