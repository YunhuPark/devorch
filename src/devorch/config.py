"""Project config / policy loading (``.devorch/config.yaml`` and ``policy.yaml``)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

DEVORCH_DIR = ".devorch"
LEVELS = ("low", "medium", "high", "critical")


class ApprovalPolicy(BaseModel):
    require_human_approval_at: str = "high"
    require_for_areas: list[str] = Field(default_factory=lambda: ["database", "payment", "permission"])

    @field_validator("require_human_approval_at")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        if v not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        return v


class SecurityPolicy(BaseModel):
    require_external_review: bool = True
    security_areas: list[str] = Field(default_factory=lambda: ["auth", "security", "permission", "payment"])


class ProductionPolicy(BaseModel):
    allow_auto_merge: bool = False


class CriticalPolicy(BaseModel):
    required_reviewers: int = 2


class LimitsPolicy(BaseModel):
    max_retries: int = 3
    command_timeout_s: int = 300


class Policy(BaseModel):
    version: int = 1
    rules: dict[str, dict[str, str]] = Field(
        default_factory=lambda: {
            "low": {"implementation": "claude"},
            "medium": {"implementation": "claude", "review": "openai"},
            "high": {"implementation": "claude", "security": "openai", "architecture": "gemini"},
            "critical": {"implementation": "claude", "security": "openai", "architecture": "gemini"},
        }
    )
    # Provider used when a role has to be added by a policy rule.
    default_roles: dict[str, str] = Field(
        default_factory=lambda: {"review": "openai", "security": "openai", "architecture": "gemini"}
    )
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    security: SecurityPolicy = Field(default_factory=SecurityPolicy)
    production: ProductionPolicy = Field(default_factory=ProductionPolicy)
    critical: CriticalPolicy = Field(default_factory=CriticalPolicy)
    limits: LimitsPolicy = Field(default_factory=LimitsPolicy)
    allowed_commands: list[str] = Field(
        default_factory=lambda: [
            "npm test", "npm run test", "npm run build", "pytest", "python -m pytest",
            "mvn test", "gradle test", "./gradlew test",
        ]
    )
    # Rough per-call planning estimates, NOT measured values. Override with real numbers.
    estimates: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "cost_usd": {"claude": 0.05, "openai": 0.03, "gemini": 0.02},
            "latency_s": {"claude": 40.0, "openai": 25.0, "gemini": 25.0},
        }
    )

    @field_validator("rules")
    @classmethod
    def _valid_rules(cls, v: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        for level in LEVELS:
            if level not in v:
                raise ValueError(f"rules.{level} is missing")
            if "implementation" not in v[level]:
                raise ValueError(f"rules.{level}.implementation is required")
        return v


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k == "rules" and isinstance(v, dict):
            out.setdefault("rules", {}).update(v)  # a level is replaced as a whole
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def devorch_dir(root: Path) -> Path:
    return Path(root) / DEVORCH_DIR


def load_policy(root: Path) -> Policy:
    """Load ``.devorch/policy.yaml`` merged over the built-in defaults."""
    default = Policy().model_dump()
    path = devorch_dir(root) / "policy.yaml"
    if not path.exists():
        return Policy.model_validate(default)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return Policy.model_validate(_merge(default, raw))


DEFAULT_CONFIG = {
    "version": 1,
    "project": {"name": None},
    "providers": {
        "claude": {"api_key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5"},
        "openai": {"api_key_env": "OPENAI_API_KEY", "model": None},
        "gemini": {"api_key_env": "GEMINI_API_KEY", "model": None},
    },
}


def load_config(root: Path) -> dict[str, Any]:
    """Load ``.devorch/config.yaml`` merged over defaults (missing file → defaults)."""
    path = devorch_dir(root) / "config.yaml"
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return _merge(DEFAULT_CONFIG, raw)


def init_project(root: Path, force: bool = False) -> list[Path]:
    """Create ``.devorch/`` with default config and policy. Returns created paths."""
    root = Path(root)
    d = devorch_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["project"]["name"] = root.resolve().name
    targets = {
        d / "config.yaml": config,
        d / "policy.yaml": Policy().model_dump(),
    }
    for path, content in targets.items():
        if path.exists() and not force:
            continue
        path.write_text(yaml.safe_dump(content, allow_unicode=True, sort_keys=False), encoding="utf-8")
        created.append(path)

    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("audit.jsonl\nruns/\n", encoding="utf-8")
        created.append(gi)
    return created
