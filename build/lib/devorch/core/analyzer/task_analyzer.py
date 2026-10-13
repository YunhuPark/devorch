"""Task Analyzer — turns a natural-language request into a structured task."""

from __future__ import annotations

from devorch.core.analyzer.keywords import (
    AREA_KEYWORDS,
    LARGE_SCOPE_HINTS,
    TYPE_KEYWORDS,
    matches,
)
from devorch.models import Area, TaskAnalysis, TaskType


def analyze_task(text: str) -> TaskAnalysis:
    text = text.strip()
    if not text:
        raise ValueError("작업 설명이 비어 있습니다.")

    hits: dict[str, list[str]] = {}
    areas: list[Area] = []
    for area, keywords in AREA_KEYWORDS.items():
        found = [k for k in keywords if matches(text, k)]
        if found:
            areas.append(area)
            hits[area.value] = found

    task_type = TaskType.CHANGE
    for candidate, keywords in TYPE_KEYWORDS:
        if any(matches(text, k) for k in keywords):
            task_type = candidate
            break

    large = any(h in text.lower() for h in LARGE_SCOPE_HINTS)
    sensitive = [a for a in areas if a not in (Area.UI, Area.USER)]
    if large or len(sensitive) >= 3:
        scope = "large"
    elif len(areas) >= 2:
        scope = "medium"
    else:
        scope = "small"

    return TaskAnalysis(text=text, task_type=task_type, areas=areas, scope=scope, keywords_hit=hits)
