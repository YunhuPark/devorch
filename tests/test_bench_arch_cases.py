"""The architecture cases added on 2026-10-09: their matchers must catch plausible findings of the seeded defect and
ignore unrelated ones. The matchers were fixed BEFORE any model saw these cases; don't tune them to model output."""

import pytest

from devorch.bench.cases import load_cases, validate_cases
from devorch.bench.metrics import judge, score_run
from devorch.models import Finding, Severity

CASES = {c.id: c for c in load_cases()}
NEW_DEFECT_CASES = ["c12_circular_import", "c13_domain_imports_web", "c14_duplicated_rule", "c15_global_cache"]


def f(title, detail="", sev="medium", cat="architecture"):
    return Finding(source="x", role="architecture", severity=Severity(sev), category=cat, title=title, detail=detail)


def test_new_cases_are_valid_architecture_defects_and_one_clean_control():
    assert validate_cases(list(CASES.values())) == []
    for cid in NEW_DEFECT_CASES:
        c = CASES[cid]
        assert [d.category for d in c.defects] == ["architecture"] and "2026-10-09" in c.notes
    ctrl = CASES["c16_clean_layered"]
    assert ctrl.clean and not ctrl.traps  # strict false-positive measurement only


@pytest.mark.parametrize(
    "cid,title,detail",
    [
        ("c12_circular_import", "서비스 간 순환 import", "user_service와 order_service가 서로를 import함"),
        ("c12_circular_import", "Circular dependency between services", ""),
        ("c12_circular_import", "ImportError 위험", "두 모듈이 서로 의존하는 순환 참조"),
        ("c13_domain_imports_web", "도메인 모델이 Flask에 의존", ""),
        ("c13_domain_imports_web", "Domain layer coupled to web framework", "uses flask.request inside the entity"),
        ("c13_domain_imports_web", "요청 컨텍스트 밖에서 호출하면 실패", "request context 가 없으면 RuntimeError"),
        ("c14_duplicated_rule", "세율이 하드코딩됨", "0.1을 직접 사용"),
        ("c14_duplicated_rule", "Duplicated tax logic", "invoice_total re-implements calc_tax"),
        ("c14_duplicated_rule", "magic number 0.1", ""),
        ("c15_global_cache", "모듈 전역 캐시", "전역 가변 상태"),
        ("c15_global_cache", "Thread-safety of the shared cache", ""),
        ("c15_global_cache", "global mutable state", "module-level dict"),
    ],
)
def test_matchers_catch_plausible_findings_of_the_seeded_defect(cid, title, detail):
    detected, _ = judge(CASES[cid], [f(title, detail)])
    assert detected, (cid, title)


@pytest.mark.parametrize(
    "cid,title,detail",
    [
        ("c12_circular_import", "단위 테스트가 없음", ""),
        ("c12_circular_import", "존재하지 않는 사용자 ID에서 KeyError", "get_user_name 이 예외를 던짐"),
        ("c13_domain_imports_web", "지원하지 않는 언어 코드 처리 누락", "ko/en 외에는 한국어로 처리"),
        ("c13_domain_imports_web", "Accept-Language 파싱이 단순함", ""),
        ("c14_duplicated_rule", "음수 금액 검증 누락", ""),
        ("c14_duplicated_rule", "float 반올림 오차 가능성", "Decimal 사용 권장"),
        ("c15_global_cache", "캐시 항목에 만료(TTL)가 없어 오래된 환율을 반환", ""),
        ("c15_global_cache", "외부 API 오류 처리 누락", "예외가 전파됨"),
        ("c15_global_cache", "캐시가 무한히 커질 수 있음", "unbounded growth"),
    ],
)
def test_matchers_do_not_fire_on_unrelated_findings(cid, title, detail):
    detected, unmatched = judge(CASES[cid], [f(title, detail, cat="correctness")])
    assert not detected and unmatched == 1


def test_a_security_finding_never_counts_for_an_architecture_defect_and_vice_versa():
    for cid in NEW_DEFECT_CASES:
        assert not judge(CASES[cid], [f("SQL 인젝션 취약점", "입력값을 쿼리에 직접 연결", sev="critical", cat="security")])[0]
    assert "sqli" not in judge(CASES["c01_sql_injection"], [f("순환 의존", "서로 import")])[0]


def test_the_clean_control_scores_like_any_clean_case():
    ctrl = CASES["c16_clean_layered"]
    assert not score_run(ctrl, "single", 0, [f("테스트 부재", sev="low")], True).false_positive
    assert score_run(ctrl, "single", 0, [f("재고 음수 처리", sev="medium")], True).false_positive  # strict rule: any MEDIUM+
    assert not score_run(ctrl, "single", 0, [f("재고 음수 처리", sev="medium")], True).trap_alarm
