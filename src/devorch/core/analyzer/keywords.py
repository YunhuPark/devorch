"""Keyword tables shared by the analyzers.

ASCII keywords match at a word start (so "login" matches "logins" but "ui" does
not match "build"). Entries starting with ``re:`` are raw regular expressions.
Non-ASCII (Korean) keywords match as substrings.
"""

from __future__ import annotations

import re

from devorch.models import Area, TaskType

AREA_KEYWORDS: dict[Area, list[str]] = {
    Area.PAYMENT: ["결제", "환불", "청구", "정산", "payment", "billing", "checkout", "refund", "invoice", "stripe"],
    Area.PERMISSION: ["권한", "인가", "역할", "permission", "rbac", "acl", "authorization", "re:\\broles?\\b"],
    Area.AUTH: [
        "인증", "로그인", "로그아웃", "비밀번호", "세션",
        "login", "logout", "jwt", "oauth", "password", "session", "sso", "signin", "authentication", "re:\\bauth(?!or)",
    ],
    Area.DATABASE: [
        "데이터베이스", "마이그레이션", "스키마", "테이블", "컬럼",
        "database", "migration", "schema", "re:\\bdb\\b", "sql", "column",
    ],
    Area.SECURITY: ["보안", "암호", "토큰", "취약점", "security", "encrypt", "secret", "token", "csrf", "xss", "vulnerab"],
    Area.DEPENDENCY: ["의존성", "라이브러리", "패키지", "dependency", "dependencies", "upgrade", "package.json", "pom.xml"],
    Area.INFRA: ["배포", "인프라", "deploy", "docker", "kubernetes", "terraform", "pipeline", "ci/cd"],
    Area.API: ["엔드포인트", "api", "endpoint", "re:\\brest\\b", "controller", "route"],
    Area.USER: ["사용자", "유저", "회원", "user", "account", "member", "signup"],
    Area.UI: ["버튼", "화면", "문구", "텍스트", "스타일", "색상", "레이아웃", "button", "ui", "css", "style", "layout", "text", "label", "page"],
}

# Risk contribution of each area (each counted once).
AREA_WEIGHTS: dict[Area, int] = {
    Area.PAYMENT: 60,
    Area.PERMISSION: 45,
    Area.AUTH: 35,
    Area.DATABASE: 30,
    Area.SECURITY: 25,
    Area.INFRA: 20,
    Area.API: 20,
    Area.DEPENDENCY: 15,
    Area.USER: 5,
    Area.UI: 0,
}

# Minimum level forced when an area is touched (see risk_engine).
AREA_FLOORS: dict[Area, str] = {
    Area.PAYMENT: "critical",
    Area.PERMISSION: "critical",
    Area.AUTH: "high",
    Area.DATABASE: "high",
}

# Path fragments used to locate files related to an area.
AREA_PATH_HINTS: dict[Area, list[str]] = {
    Area.PAYMENT: ["payment", "billing", "checkout", "refund", "invoice"],
    Area.PERMISSION: ["permission", "role", "acl", "rbac", "authoriz"],
    Area.AUTH: ["auth", "login", "jwt", "session", "security", "token"],
    Area.DATABASE: ["repository", "migration", "schema", "entity", "dao", "sql", "model"],
    Area.SECURITY: ["security", "crypto", "secret", "token"],
    Area.USER: ["user", "account", "member"],
    Area.API: ["controller", "route", "api", "endpoint", "handler"],
    Area.UI: ["component", "view", "page", "style"],
    Area.INFRA: ["docker", "deploy", "terraform"],
    Area.DEPENDENCY: [],
}

# Path fragments that mark security / database sensitive files in impact results.
SECURITY_PATH_HINTS = ["auth", "security", "permission", "role", "acl", "jwt", "token", "payment", "crypto", "secret", "login"]
DATABASE_PATH_HINTS = ["repository", "migration", "schema", "entity", "dao", ".sql", "/db/", "database"]

TYPE_KEYWORDS: list[tuple[TaskType, list[str]]] = [
    (TaskType.BUGFIX, ["버그", "오류", "에러", "고쳐", "수정해야", "bug", "re:\\bfix(es|ed)?\\b", "error", "crash", "broken"]),
    (TaskType.REFACTOR, ["리팩토링", "리팩터", "정리", "refactor", "cleanup", "restructure"]),
    (TaskType.TEST, ["테스트 추가", "테스트 작성", "re:\\badd tests?\\b", "write test", "unit test"]),
    (TaskType.DOCS, ["문서", "주석", "readme", "docs", "documentation", "comment"]),
    (TaskType.FEATURE, ["추가", "만들", "구현", "신규", "re:\\badd\\b", "create", "implement", "build", "new feature"]),
    (TaskType.CHANGE, ["변경", "수정", "바꿔", "교체", "update", "change", "modify", "replace", "migrate"]),
]

LARGE_SCOPE_HINTS = ["전체", "모든", "전면", "시스템", "all ", "entire", "whole", "across", "overhaul", "rewrite"]


def matches(text: str, keyword: str) -> bool:
    """Case-insensitive keyword match following the rules in the module docstring."""
    text = text.lower()
    if keyword.startswith("re:"):
        return re.search(keyword[3:], text) is not None
    if keyword.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(keyword.lower()), text) is not None
    return keyword in text
