"""YAML configuration for lanes, gates, WIP ceiling, and model tiers.

Operational settings are loaded from an external YAML file so the private
harness does not hardcode them. ``api_key_env`` stores the *name* of an
environment variable holding the API key — never the secret value itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator


def _import_yaml():
    try:
        import yaml
    except ImportError as e:
        raise ImportError(
            "PyYAML is required for config loading. "
            "Install with: pip install nousergon-groomer[config]"
        ) from e
    return yaml


class LaneConfig(BaseModel):
    name: str
    label: str


class GateFamily(BaseModel):
    prefix: str
    families: list[str]


class ModelConfig(BaseModel):
    provider: str = "openai-compatible"
    base_url: str
    api_key_env: str
    model: str
    temperature: float = 0.0


class GroomerConfig(BaseModel):
    wip_ceiling: int
    lanes: list[LaneConfig] = []
    gates: list[GateFamily] = []
    model: Optional[ModelConfig] = None
    do_not_groom_label: str = "do-not-groom"

    @field_validator("wip_ceiling")
    @classmethod
    def _wip_ceiling_at_least_one(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"wip_ceiling must be >= 1 (got {v}); a ceiling below 1 "
                "would make the loop unable to admit any work (§4.1)"
            )
        return v


DEFAULT_CONFIG = GroomerConfig(
    wip_ceiling=5,
    lanes=[
        LaneConfig(name="scanner-remediation", label="scanner-remediation"),
        LaneConfig(name="standing-exception", label="standing-exception"),
        LaneConfig(name="groom-reviewed", label="groom-reviewed"),
    ],
    gates=[
        GateFamily(
            prefix="gate:",
            families=["weekly-sf", "preopen-sf", "postclose-sf"],
        ),
    ],
    do_not_groom_label="do-not-groom",
)


def load_config(path: Path) -> GroomerConfig:
    """Load and validate a YAML config file."""
    yaml = _import_yaml()
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in config file {path}: {e}") from e
    if data is None:
        raise ValueError(f"Config file {path} is empty")
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file {path} must contain a YAML mapping, got {type(data).__name__}"
        )
    return GroomerConfig.model_validate(data)


def write_default_config(path: Path) -> None:
    """Write :data:`DEFAULT_CONFIG` to ``path`` (for ``groomer init``)."""
    yaml = _import_yaml()
    data = DEFAULT_CONFIG.model_dump(mode="python", exclude_none=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
