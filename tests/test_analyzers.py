from pathlib import Path

import pytest

from devorch.core.analyzer.impact_analyzer import analyze_impact, build_graph
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.models import Area, RiskLevel, TaskType


# ---- Task Analyzer -------------------------------------------------------
def test_empty_task_rejected():
    with pytest.raises(ValueError):
        analyze_task("   ")


@pytest.mark.parametrize(
    "text,task_type,area",
    [
        ("로그인 기능에 JWT 인증 추가해줘", TaskType.FEATURE, Area.AUTH),
        ("결제 API 변경", TaskType.CHANGE, Area.PAYMENT),
        ("Fix login crash on empty password", TaskType.BUGFIX, Area.AUTH),
        ("권한 정책 변경", TaskType.CHANGE, Area.PERMISSION),
        ("users 테이블 마이그레이션 작성", None, Area.DATABASE),
        ("README 문서 업데이트", TaskType.DOCS, None),
    ],
)
def test_task_analysis(text, task_type, area):
    a = analyze_task(text)
    if task_type:
        assert a.task_type is task_type
    if area:
        assert area in a.areas


def test_no_false_positive_keywords():
    # "build" must not trigger UI ("ui"), "address" must not look like "add", "author" is not auth
    a = analyze_task("update the author address column label")
    assert Area.AUTH not in a.areas
    a2 = analyze_task("rebuild everything")
    assert Area.UI not in a2.areas


# ---- Risk Engine (levels from the design doc table) ---------------------
@pytest.mark.parametrize(
    "text,level",
    [
        ("버튼 텍스트 변경", RiskLevel.LOW),
        ("회원가입 UI 수정", RiskLevel.LOW),
        ("일반 API 추가", RiskLevel.MEDIUM),
        ("DB schema 변경", RiskLevel.HIGH),
        ("인증 변경", RiskLevel.HIGH),
        ("결제 로직 변경", RiskLevel.CRITICAL),
        ("권한 정책 변경", RiskLevel.CRITICAL),
    ],
)
def test_risk_levels_match_design_table(text, level):
    assert assess_risk(analyze_task(text)).level is level


def test_floor_overrides_low_score():
    r = assess_risk(analyze_task("권한 문구 수정"))
    assert r.level is RiskLevel.CRITICAL
    assert r.floor_reason


def test_docs_do_not_go_negative():
    r = assess_risk(analyze_task("README 문서 업데이트"))
    assert r.score >= 0 and r.level is RiskLevel.LOW


# ---- Impact Analyzer -----------------------------------------------------
def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_java_dependents_are_indirect(tmp_path):
    _write(tmp_path, "a/UserService.java", "class UserService {}")
    _write(tmp_path, "a/AuthController.java", "class AuthController { UserService u; }")
    _write(tmp_path, "a/OrderService.java", "class OrderService { AuthController c; }")
    _write(tmp_path, "a/Unrelated.java", "class Unrelated {}")
    rep = analyze_impact(tmp_path, analyze_task("UserService.java 수정"))
    assert rep.direct_files == ["a/UserService.java"]
    assert rep.indirect_files == ["a/AuthController.java", "a/OrderService.java"]
    assert rep.indirect_depth == {"a/AuthController.java": 1, "a/OrderService.java": 2}
    assert "a/Unrelated.java" not in rep.indirect_files


def test_python_absolute_and_relative_imports(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/auth.py", "def login(): ...")
    _write(tmp_path, "pkg/views.py", "from .auth import login\n")
    _write(tmp_path, "app.py", "from pkg.views import render\nimport pkg.auth\n")
    imports, _ = build_graph(tmp_path)
    assert imports["pkg/views.py"] == {"pkg/auth.py"}
    assert {"pkg/views.py", "pkg/auth.py"} <= imports["app.py"]


def test_js_relative_imports(tmp_path):
    _write(tmp_path, "src/auth/login.ts", "export const login = 1")
    _write(tmp_path, "src/pages/Home.tsx", "import { login } from '../auth/login'")
    _write(tmp_path, "src/index.ts", "import Home from './pages/Home'")
    rep = analyze_impact(tmp_path, analyze_task("로그인 변경"))
    assert "src/auth/login.ts" in rep.direct_files
    assert set(rep.indirect_files) == {"src/pages/Home.tsx", "src/index.ts"}
    assert rep.security_impact


def test_specific_area_suppresses_generic_hints(tmp_path):
    _write(tmp_path, "AuthController.java", "class AuthController {}")
    _write(tmp_path, "PaymentService.java", "class PaymentService {}")
    rep = analyze_impact(tmp_path, analyze_task("결제 API 수정"))
    assert rep.direct_files == ["PaymentService.java"]


def test_ignored_dirs_and_empty_project(tmp_path):
    _write(tmp_path, "node_modules/x/login.js", "x")
    rep = analyze_impact(tmp_path, analyze_task("로그인 변경"))
    assert rep.files_scanned == 0 and rep.notes


def test_unknown_impact_is_reported_not_guessed(tmp_path):
    _write(tmp_path, "Foo.java", "class Foo {}")
    rep = analyze_impact(tmp_path, analyze_task("버튼 텍스트 변경"))
    assert rep.direct_files == [] and "찾지 못했습니다" in rep.notes[0]


def test_impact_raises_risk(tmp_path):
    for i in range(10):
        _write(tmp_path, f"auth/A{i}.java", f"class A{i} {{}}")
    task = analyze_task("로그인 변경")
    base = assess_risk(task).score
    with_impact = assess_risk(task, analyze_impact(tmp_path, task)).score
    assert with_impact > base
