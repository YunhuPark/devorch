"""English tokens touching Korean (a particle like "에서", or a noun like "주문DB") must still be recognised.

Found 2026-11-24 (agent test report §6): "DB에서 주문 조회" got no area and LOW, while "DB 에서 주문 조회" (one space) got DATABASE and HIGH —
the ``re:`` rules used Python's Unicode ``\\b``, which treats Hangul as word characters. Now ``re:`` rules match with ASCII boundaries.
"""

import pytest

from devorch.core.analyzer.keywords import matches
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.models import Area, RiskLevel, TaskType


@pytest.mark.parametrize("text", ["DB에서 주문 조회", "DB 에서 주문 조회", "주문DB 컬럼 정리", "db를 바꿔"])
def test_db_with_or_without_a_korean_particle_is_the_database_area_and_at_least_high(text):
    a = analyze_task(text)
    assert Area.DATABASE in a.areas
    assert assess_risk(a).level in (RiskLevel.HIGH, RiskLevel.CRITICAL)


def test_the_space_no_longer_changes_the_risk_level():
    assert assess_risk(analyze_task("DB에서 주문 조회")).level is assess_risk(analyze_task("DB 에서 주문 조회")).level


@pytest.mark.parametrize(
    "text, keyword",
    [("REST에 엔드포인트 추가", "re:\\brest\\b"), ("roles를 바꿔", "re:\\broles?\\b"), ("auth에서 검사", "re:\\bauth(?!or)"),
     ("fixed된 버그", "re:\\bfix(es|ed)?\\b"), ("add tests해줘", "re:\\badd tests?\\b")],
)
def test_every_regex_rule_accepts_a_korean_neighbour(text, keyword):
    assert matches(text, keyword)


@pytest.mark.parametrize(
    "text, keyword",
    [("dbg 로그 추가", "re:\\bdb\\b"), ("mydb 정리", "re:\\bdb\\b"), ("restore 기능", "re:\\brest\\b"), ("author 표시", "re:\\bauth(?!or)"),
     ("address 수정", "re:\\badd\\b"), ("prefixed 이름", "re:\\bfix(es|ed)?\\b")],
)
def test_english_words_that_only_contain_the_token_still_do_not_match(text, keyword):
    assert not matches(text, keyword)


def test_a_korean_only_task_is_unaffected():
    a = analyze_task("버튼 문구 수정")
    assert a.areas == [Area.UI] and a.task_type is TaskType.CHANGE
