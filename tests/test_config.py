"""Contract tests for the YAML configuration system (issue #24)."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nousergon_groomer.config import (
    DEFAULT_CONFIG,
    GroomerConfig,
    LaneConfig,
    ModelConfig,
    load_config,
    write_default_config,
)

VALID_YAML = """\
wip_ceiling: 3
lanes:
  - name: scanner-remediation
    label: scanner-remediation
gates:
  - prefix: "gate:"
    families: [weekly-sf]
do_not_groom_label: skip-me
model:
  provider: openai-compatible
  base_url: https://example.com/v1
  api_key_env: GROOMER_API_KEY
  model: gpt-4o-mini
  temperature: 0.1
"""


def test_valid_yaml_loads(tmp_path: Path):
    path = tmp_path / "groomer.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.wip_ceiling == 3
    assert cfg.lanes == [LaneConfig(name="scanner-remediation", label="scanner-remediation")]
    assert cfg.gates[0].prefix == "gate:"
    assert cfg.gates[0].families == ["weekly-sf"]
    assert cfg.do_not_groom_label == "skip-me"
    assert cfg.model is not None
    assert cfg.model.base_url == "https://example.com/v1"
    assert cfg.model.api_key_env == "GROOMER_API_KEY"
    assert cfg.model.model == "gpt-4o-mini"
    assert cfg.model.temperature == 0.1


def test_wip_ceiling_below_one_raises():
    with pytest.raises(ValidationError):
        GroomerConfig(wip_ceiling=0)


def test_missing_required_fields_raise(tmp_path: Path):
    path = tmp_path / "empty.yaml"
    path.write_text("lanes: []\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_write_default_config_round_trip(tmp_path: Path):
    path = tmp_path / "groomer.yaml"
    write_default_config(path)
    cfg = load_config(path)
    assert cfg.wip_ceiling == DEFAULT_CONFIG.wip_ceiling
    assert cfg.lanes == DEFAULT_CONFIG.lanes
    assert cfg.gates == DEFAULT_CONFIG.gates
    assert cfg.do_not_groom_label == DEFAULT_CONFIG.do_not_groom_label
    assert cfg.model is None


def test_default_config_lanes_and_gates():
    assert [lane.name for lane in DEFAULT_CONFIG.lanes] == [
        "scanner-remediation",
        "standing-exception",
        "groom-reviewed",
    ]
    assert DEFAULT_CONFIG.gates[0].prefix == "gate:"
    assert DEFAULT_CONFIG.gates[0].families == [
        "weekly-sf",
        "preopen-sf",
        "postclose-sf",
    ]


def test_model_optional():
    cfg = GroomerConfig(wip_ceiling=1)
    assert cfg.model is None


def test_invalid_yaml_raises(tmp_path: Path):
    path = tmp_path / "bad.yaml"
    path.write_text("wip_ceiling: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid YAML"):
        load_config(path)


def test_api_key_env_stores_env_var_name_not_secret(tmp_path: Path):
    path = tmp_path / "model.yaml"
    path.write_text(
        """\
wip_ceiling: 1
model:
  base_url: https://example.com/v1
  api_key_env: MY_SECRET_ENV_VAR
  model: test-model
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.model is not None
    assert cfg.model.api_key_env == "MY_SECRET_ENV_VAR"
    dumped = cfg.model.model_dump_json()
    assert "sk-" not in dumped
    assert "MY_SECRET_ENV_VAR" in dumped


def test_model_config_defaults():
    model = ModelConfig(
        base_url="https://example.com/v1",
        api_key_env="GROOMER_API_KEY",
        model="test",
    )
    assert model.provider == "openai-compatible"
    assert model.temperature == 0.0
