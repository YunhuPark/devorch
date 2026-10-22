"""Project config / policy loading (``.devorch/config.yaml`` and ``policy.yaml``)."""

from __future__ import annotations

import copy
import math
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

DEVORCH_DIR = ".devorch"
LEVELS = ("low", "medium", "high", "critical")


class ApprovalPolicy(BaseModel):
    require_human_approval_at: str = "high"
    require_for_areas: list[str] = Field(default_factory=lambda: ["database", "payment", "permission"])
    # Off by default: with sandbox mode "none" the tests are the agent's own code running on this PC, and a hostile one can read the signing key and
    # approve itself (reproduced 2026-10-15). On = tests of changes that need a human approval are refused unless the Docker sandbox is configured.
    require_sandbox_for_approval: bool = False

    @field_validator("require_human_approval_at")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        if v not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        return v


class ProtectedPath(BaseModel):
    """A path the project OWNER declares sensitive. A diff that touches it is raised to at least ``min_level`` (never lowered).

    DevOrch can only guess from keywords and path hints (``auth``, ``payment``, ``/db/`` ...); it cannot know that ``src/db.py`` holds the
    publish queue of an auto-posting service. The owner can, so the owner says so. Matching (``core/analyzer/diff_reassess.py``): a pattern ending in
    ``/`` is a folder (everything below it); a pattern without ``/`` is matched against the file NAME anywhere; otherwise ``fnmatch`` on the
    repo-relative path with ``/`` separators (``*`` also crosses ``/``).
    """

    pattern: str
    min_level: str = "high"

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        v = v.strip().replace("\\", "/")
        while v.startswith("./"):
            v = v[2:]
        if not v or v.startswith("/") or ".." in v.split("/"):
            raise ValueError("pattern must be a non-empty repo-relative path (no leading '/', no '..')")
        return v

    @field_validator("min_level")
    @classmethod
    def _level(cls, v: str) -> str:
        if v not in LEVELS[1:]:
            raise ValueError(f"min_level must be one of {LEVELS[1:]} (low would change nothing)")
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
    # Per-task spend cap in USD (null = no cap). Needs prices for every provider in use
    # (providers.<name>.input_per_mtok / output_per_mtok) — without them the cap can't be enforced, so runs refuse to start.
    max_cost_usd: float | None = None
    # Output budget per reviewer call. Thinking models (Gemini) spend part of it on thoughts: 4096 was too small for a ~9k-token diff
    # (the review was cut off and still billed). A higher budget costs nothing unless the model actually uses it.
    review_max_tokens: int = 8192
    # Output budget for the implementation call (it writes the whole diff). 4096 would cut off a multi-file change.
    implementation_max_tokens: int = 16384

    @field_validator("review_max_tokens", "implementation_max_tokens")
    @classmethod
    def _output_budget(cls, v: int) -> int:
        if not 1024 <= v <= 65536:
            raise ValueError("output token budgets (limits.review_max_tokens / implementation_max_tokens) must be between 1024 and 65536")
        return v

    @field_validator("max_cost_usd")
    @classmethod
    def _positive_cost(cls, v: float | None) -> float | None:
        # NaN compares False with everything, so "spent >= nan" would never stop anything; inf never triggers either.
        if v is not None and not (math.isfinite(v) and v > 0):
            raise ValueError("limits.max_cost_usd must be a finite number > 0 (or null for no cap)")
        return v


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
    protected_paths: list[ProtectedPath] = Field(default_factory=list)
    # Content check (core/analyzer/test_weakening.py): a diff that deletes/skips/trivialises tests is raised to MEDIUM (one kind of signal) or HIGH (two). Raise-only.
    detect_weakened_tests: bool = True
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
    # Planning estimates. "cost_usd" is the flat per-call FALLBACK (unmeasured; ~7-8x too high for reviewer calls on small
    # diffs) used for the implementation step and for providers without prices; reviewer calls of priced providers are
    # estimated from tokens x price instead (core/router/cost_model.py). "assumed" holds inputs that are assumptions.
    estimates: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "cost_usd": {"claude": 0.05, "openai": 0.03, "gemini": 0.02},
            "latency_s": {"claude": 40.0, "openai": 25.0, "gemini": 25.0},
            "assumed": {"diff_chars_per_file": 1500.0},
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


def load_test_env(config: dict, host_env: dict[str, str] | None = None) -> dict[str, str]:
    """``project.test_env``: DUMMY values the test run needs (e.g. a repo whose tests only start when ``SOME_API_KEY`` is set to anything).

    DevOrch strips key/token/secret-looking variables from test runs; this is the explicit way to give a test run a placeholder. It is
    meant for dummy values and is checked only as far as possible: DevOrch cannot tell a real key from a dummy in general, so it rejects
    (a) values in a known credential format, (b) a value equal to the same-named variable in the real environment (a copied real key),
    and (c) anything but plain strings/numbers. A real secret still must never be written to config.yaml.
    """
    from devorch.security.secret_redactor import has_secret_literal  # local: keeps config import-light

    raw = (config.get("project") or {}).get("test_env")
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("project.test_env 는 '이름: 값' 목록이어야 합니다")
    host = os.environ if host_env is None else host_env
    out: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            raise ConfigError(f"project.test_env 의 이름이 환경변수 이름이 아닙니다: {name!r}")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ConfigError(f"project.test_env.{name} 는 문자열 또는 숫자여야 합니다")
        text = str(value)
        if has_secret_literal(text) or (host.get(name) and host[name] == text):
            raise ConfigError(
                f"project.test_env.{name} 의 값이 실제 키처럼 보입니다. 여기에는 가짜 값(예: test-key)만 쓰세요 — "
                "진짜 키는 config.yaml 에 쓰지 않습니다."
            )
        out[name] = text
    return out


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """A config/policy file the user has to fix. A ValueError (callers that catch it keep working); the CLI (``cli/app.py``) turns it
    into one line and exit code 2 for every command, also those that don't catch it."""


def _read_mapping(path: Path) -> dict[str, Any]:
    """The YAML mapping in ``path``. Every way a hand-edited file can be wrong becomes a ValueError the CLI reports in one line
    (a YAMLError or UnicodeDecodeError used to escape as a traceback)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except UnicodeDecodeError as e:
        raise ConfigError(
            f"{path} 을(를) UTF-8 로 읽을 수 없습니다(바이트 {e.start} 근처). 편집기에서 '다른 이름으로 저장' 때 인코딩을 UTF-8 로 지정해 다시 저장하세요."
        ) from e
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" ({mark.line + 1}번째 줄, {mark.column + 1}번째 칸)" if mark is not None else ""
        problem = getattr(e, "problem", None) or "YAML 문법 오류"
        raise ConfigError(f"{path} 의 YAML 문법이 올바르지 않습니다{where}: {problem}. 들여쓰기는 공백만 쓰고(탭 불가), `키: 값` 의 콜론 뒤에 공백이 필요합니다.") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return raw


def load_policy(root: Path) -> Policy:
    """Load ``.devorch/policy.yaml`` merged over the built-in defaults."""
    default = Policy().model_dump()
    path = devorch_dir(root) / "policy.yaml"
    if not path.exists():
        return Policy.model_validate(default)
    try:
        return Policy.model_validate(_merge(default, _read_mapping(path)))
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:5])
        raise ConfigError(f"{path} 의 값이 올바르지 않습니다 — {problems}") from e


DEFAULT_CONFIG = {
    "version": 1,
    "project": {"name": None},
    # Prices are USD per million tokens and are never guessed: null = not given (calls still work, their cost is "unknown").
    "providers": {
        "claude": {"api_key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5", "input_per_mtok": None, "output_per_mtok": None},
        "openai": {"api_key_env": "OPENAI_API_KEY", "model": None, "input_per_mtok": None, "output_per_mtok": None},
        "gemini": {"api_key_env": "GEMINI_API_KEY", "model": None, "input_per_mtok": None, "output_per_mtok": None},
    },
}

CONFIG_HEADER = """# DevOrch 설정. API 키는 여기에 쓰지 마세요 — api_key_env 에 적힌 이름의 환경변수로만 넣습니다.
# model: 사용할 모델 ID. claude 는 기본값이 있고, openai/gemini 는 직접 적어야 합니다(DevOrch 는 모델명을 추측하지 않습니다).
# input_per_mtok / output_per_mtok: 100만 토큰당 USD 단가. 공급사 가격 페이지에서 확인해 적으세요(추측하지 않습니다).
#   비워 두면(null) 호출은 되지만 비용이 '알 수 없음'으로 표시되고, 작업당 비용 상한(policy.yaml 의 limits.max_cost_usd)을 쓸 수 없습니다.
# 쓰지 않을 provider 는 그대로 두면 됩니다(키가 없으면 건너뜁니다). 상태 확인: devorch doctor
"""


def load_config(root: Path) -> dict[str, Any]:
    """Load ``.devorch/config.yaml`` merged over defaults (missing file → defaults)."""
    path = devorch_dir(root) / "config.yaml"
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    return _merge(DEFAULT_CONFIG, _read_mapping(path))


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
        header = CONFIG_HEADER if path.name == "config.yaml" else ""
        path.write_text(header + yaml.safe_dump(content, allow_unicode=True, sort_keys=False), encoding="utf-8")
        created.append(path)

    gi = d / ".gitignore"
    existed = gi.exists()
    ensure_gitignore(d)
    if not existed and gi.exists():
        created.append(gi)
    return created


# DevOrch's own local state: never something to commit (task records hold diffs and signed approvals tied to a local key).
GITIGNORE_ENTRIES = ("audit.jsonl", "audit.head", "audit.jsonl.lock", "runs/", "worktrees/", "tasks/")


def ensure_gitignore(d: Path) -> None:
    """Make sure ``.devorch/.gitignore`` lists every local-state file DevOrch writes, adding what an older version of the file lacks.
    Projects initialised before ``audit.head`` / ``audit.jsonl.lock`` existed otherwise show them as untracked (seen in the first real-client run)."""
    gi = Path(d) / ".gitignore"
    try:
        have = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        missing = [e for e in GITIGNORE_ENTRIES if e not in have]
        if missing:
            gi.parent.mkdir(parents=True, exist_ok=True)
            gi.write_text("\n".join([*have, *missing]) + "\n", encoding="utf-8")
    except OSError:
        pass  # read-only checkout: the entries are a convenience, never a reason to fail
