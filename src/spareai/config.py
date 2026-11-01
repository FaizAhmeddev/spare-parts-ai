"""Configuration loading with ${ENV_VAR:default} interpolation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _find_root() -> Path:
    """Project root: $SPAREAI_HOME, else the current folder if it has
    config/settings.yaml (installed package / Docker), else the source tree."""
    env = os.environ.get("SPAREAI_HOME")
    if env:
        return Path(env).resolve()
    if (Path.cwd() / "config" / "settings.yaml").exists():
        return Path.cwd().resolve()
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _find_root()
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.yaml"
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) or "")
        out = _ENV_PATTERN.sub(repl, value)
        return out
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


@dataclass
class Settings:
    raw: dict
    root: Path = field(default=PROJECT_ROOT)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node in ("", None) else node

    def path(self, dotted: str) -> Path:
        p = Path(self.get(dotted))
        return p if p.is_absolute() else self.root / p

    def override(self, dotted: str, value: Any) -> None:
        node = self.raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


def load_settings(path: str | Path | None = None) -> Settings:
    cfg_path = Path(path or os.environ.get("SPAREAI_CONFIG", DEFAULT_CONFIG))
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    # relative paths in the config resolve against the folder that holds config/
    root = cfg_path.resolve().parent.parent if path else PROJECT_ROOT
    return Settings(raw=_interpolate(raw), root=root)
