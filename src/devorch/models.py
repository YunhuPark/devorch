"""Shared domain models for DevOrch."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    FEATURE = "feature"
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    CHANGE = "change"
    TEST = "test"
    DOCS = "docs"


class Area(str, Enum):
    PAYMENT = "payment"
    PERMISSION = "permission"
    AUTH = "auth"
    DATABASE = "database"
    SECURITY = "security"
    DEPENDENCY = "dependency"
    INFRA = "infra"
    API = "api"
    USER = "user"
    UI = "ui"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["low", "medium", "high", "critical"].index(self.value)

    def __ge__(self, other: "RiskLevel") -> bool:  # type: ignore[override]
        return self.rank >= other.rank

    def __gt__(self, other: "RiskLevel") -> bool:  # type: ignore[override]
        return self.rank > other.rank

    def __le__(self, other: "RiskLevel") -> bool:  # type: ignore[override]
        return self.rank <= other.rank

    def __lt__(self, other: "RiskLevel") -> bool:  # type: ignore[override]
        return self.rank < other.rank


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["info", "low", "medium", "high", "critical"].index(self.value)

    @property
    def blocking(self) -> bool:
        return self.rank >= Severity.HIGH.rank


class TaskAnalysis(BaseModel):
    text: str
    task_type: TaskType
    areas: list[Area] = Field(default_factory=list)
    scope: str = "small"  # small | medium | large
    keywords_hit: dict[str, list[str]] = Field(default_factory=dict)


class RiskFactor(BaseModel):
    name: str
    points: int
    reason: str


class RiskAssessment(BaseModel):
    score: int
    level: RiskLevel
    factors: list[RiskFactor] = Field(default_factory=list)
    floor_reason: str | None = None  # set when a rule forced a minimum level


class ImpactReport(BaseModel):
    files_scanned: int = 0
    direct_files: list[str] = Field(default_factory=list)
    indirect_files: list[str] = Field(default_factory=list)
    indirect_depth: dict[str, int] = Field(default_factory=dict)
    security_impact: bool = False
    database_impact: bool = False
    notes: list[str] = Field(default_factory=list)


class RoutingStep(BaseModel):
    role: str  # implementation | review | security | architecture | ...
    provider: str
    purpose: str


class CostLine(BaseModel):
    role: str
    provider: str
    usd: float
    basis: str  # "토큰×단가" | "토큰×단가(미측정 가정)" | "기본 고정값"
    input_tokens: int = 0
    output_tokens: int = 0


class RoutingPlan(BaseModel):
    level: RiskLevel
    steps: list[RoutingStep]
    requires_human_approval: bool
    approval_reasons: list[str] = Field(default_factory=list)
    run_tests: bool = True
    verification_level: str = "BASIC"
    estimated_cost_usd: float = 0.0
    estimated_latency_s: float = 0.0
    cost_breakdown: list[CostLine] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def review_steps(self) -> list[RoutingStep]:
        return [s for s in self.steps if s.role != "implementation"]

    @property
    def implementation_step(self) -> RoutingStep:
        return next(s for s in self.steps if s.role == "implementation")


class Finding(BaseModel):
    source: str  # provider name that raised it (comma-joined after dedupe)
    role: str
    severity: Severity
    category: str
    title: str
    detail: str = ""
    file: str | None = None


class Evaluation(BaseModel):
    status: str  # READY | READY_WITH_WARNINGS | NOT_READY
    blocking: list[Finding] = Field(default_factory=list)
    warnings: list[Finding] = Field(default_factory=list)
    missing_reviews: list[str] = Field(default_factory=list)
    summary: str = ""
