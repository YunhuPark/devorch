import pytest
import yaml

from devorch.config import Policy, init_project, load_policy
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.router.model_router import plan_routing
from devorch.models import ImpactReport, RiskLevel


def plan_for(text, policy=None, impact=None):
    policy = policy or Policy()
    a = analyze_task(text)
    r = assess_risk(a, impact)
    return plan_routing(a, r, impact, policy)


def roles(plan):
    return {s.role: s.provider for s in plan.steps}


def test_low_single_model_no_approval():
    p = plan_for("버튼 텍스트 변경")
    assert p.level is RiskLevel.LOW
    assert roles(p) == {"implementation": "claude"}
    assert not p.requires_human_approval
    assert p.verification_level == "BASIC"


def test_medium_adds_review():
    p = plan_for("일반 API 추가")
    assert p.level is RiskLevel.MEDIUM
    assert roles(p) == {"implementation": "claude", "review": "openai"}
    assert not p.requires_human_approval


def test_high_security_and_architecture_with_approval():
    p = plan_for("로그인 기능에 JWT 인증 추가해줘")
    assert roles(p) == {"implementation": "claude", "security": "openai", "architecture": "gemini"}
    assert p.requires_human_approval
    assert p.verification_level == "STRICT+HUMAN"


def test_critical_has_two_independent_reviewers():
    p = plan_for("결제 로직 변경")
    reviewers = {s.provider for s in p.review_steps}
    assert p.level is RiskLevel.CRITICAL and len(reviewers) >= 2
    assert p.requires_human_approval


def test_security_review_added_even_at_medium_when_policy_requires():
    # permission → forced critical anyway; use payment-free security keyword at medium
    p = plan_for("토큰 만료 문구 수정")
    assert "security" in roles(p)


def test_database_requires_approval_via_area_rule():
    p = plan_for("DB schema 변경")
    assert p.requires_human_approval
    assert any("database" in r for r in p.approval_reasons)


def test_critical_with_too_few_reviewers_is_flagged():
    pol = Policy.model_validate(
        {
            "critical": {"required_reviewers": 3},
            "default_roles": {"review": "openai", "security": "openai", "architecture": "gemini"},
        }
    )
    p = plan_for("결제 로직 변경", pol)
    assert any("독립 리뷰어" in n for n in p.notes)


def test_cost_uses_estimates_and_parallel_latency():
    p = plan_for("로그인 기능에 JWT 인증 추가해줘")
    assert p.estimated_cost_usd == pytest.approx(0.05 + 0.03 + 0.02)
    assert p.estimated_latency_s == pytest.approx(40 + 25)  # impl + slowest parallel reviewer


def test_docs_skip_tests():
    assert plan_for("README 문서 업데이트").run_tests is False


def test_impact_database_triggers_approval():
    impact = ImpactReport(database_impact=True)
    p = plan_for("버튼 텍스트 변경", impact=impact)
    assert p.requires_human_approval


# ---- config / policy files ----------------------------------------------
def test_init_creates_files_and_is_idempotent(tmp_path):
    created = init_project(tmp_path)
    names = {p.name for p in created}
    assert {"config.yaml", "policy.yaml", ".gitignore"} <= names
    assert init_project(tmp_path) == []  # nothing overwritten
    assert load_policy(tmp_path).rules["high"]["security"] == "openai"


def test_user_policy_overrides_merge(tmp_path):
    init_project(tmp_path)
    (tmp_path / ".devorch" / "policy.yaml").write_text(
        yaml.safe_dump({"rules": {"medium": {"implementation": "gemini"}}, "approval": {"require_human_approval_at": "medium"}}),
        encoding="utf-8",
    )
    pol = load_policy(tmp_path)
    assert pol.rules["medium"] == {"implementation": "gemini"}  # level replaced wholesale
    assert pol.rules["high"]["security"] == "openai"            # others untouched
    assert pol.approval.require_human_approval_at == "medium"
    assert pol.approval.require_for_areas  # nested default preserved
    assert plan_for("일반 API 추가", pol).requires_human_approval


def test_invalid_policy_rejected(tmp_path):
    init_project(tmp_path)
    (tmp_path / ".devorch" / "policy.yaml").write_text("approval:\n  require_human_approval_at: extreme\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_policy(tmp_path)
    (tmp_path / ".devorch" / "policy.yaml").write_text("rules:\n  low:\n    review: openai\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_policy(tmp_path)
