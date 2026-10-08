"""Change Impact Analyzer.

Builds a lightweight file-level dependency graph (Python / JS / TS / Java /
Kotlin) and walks it backwards: files that depend on the files a task is
likely to change are reported as indirectly impacted.

This is heuristic static analysis (regex based, no type resolution). It is meant
to surface "this change may reach X" early, not to be a precise call graph.
"""

from __future__ import annotations

import os
import re
from collections import deque
from pathlib import Path

from devorch.core.analyzer.keywords import (
    AREA_PATH_HINTS,
    DATABASE_PATH_HINTS,
    SECURITY_PATH_HINTS,
)
from devorch.models import Area, ImpactReport, TaskAnalysis

SOURCE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".java", ".kt"}
IGNORED_DIRS = {
    ".git", ".devorch", ".venv", "venv", "node_modules", "build", "dist", "target",
    "__pycache__", ".idea", ".gradle", ".next", ".pytest_cache", "out",
}
MAX_FILES = 5000
MAX_BYTES = 500_000

_PY_FROM = re.compile(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+(.+)$", re.M)
_PY_IMPORT = re.compile(r"^\s*import\s+([\w., ]+)$", re.M)
_JS_REL = re.compile(r"""(?:from\s+|require\(\s*|import\(\s*|import\s+)['"](\.{1,2}/[^'"]*|\.{1,2})['"]""")
_JVM_TOKEN = re.compile(r"\b[A-Z][A-Za-z0-9_]+\b")
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "add", "make", "create", "update",
    "change", "fix", "new", "feature", "function", "code", "file", "support",
    "java", "kotlin", "python", "javascript", "typescript", "class", "module", "method",
}


def _walk(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix in SOURCE_EXTS:
                files.append(p)
                if len(files) >= MAX_FILES:
                    return files
    return files


def _read(p: Path) -> str:
    try:
        if p.stat().st_size > MAX_BYTES:
            return ""
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def build_graph(root: Path) -> tuple[dict[str, set[str]], list[str]]:
    """Return (imports, files) where imports[a] = set of files that ``a`` depends on."""
    root = root.resolve()
    paths = _walk(root)
    rel = {p: p.relative_to(root).as_posix() for p in paths}
    files = sorted(rel.values())
    fileset = set(files)

    # Python dotted-name index: every dotted suffix of the path maps to the file.
    py_index: dict[str, set[str]] = {}
    jvm_index: dict[str, set[str]] = {}
    for r in files:
        pth = Path(r)
        if pth.suffix == ".py":
            parts = list(pth.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for i in range(len(parts)):
                if parts[i:]:
                    py_index.setdefault(".".join(parts[i:]), set()).add(r)
        elif pth.suffix in (".java", ".kt"):
            jvm_index.setdefault(pth.stem, set()).add(r)

    imports: dict[str, set[str]] = {r: set() for r in files}

    def js_resolve(src_rel: str, spec: str) -> str | None:
        base = (Path(src_rel).parent / spec)
        norm = os.path.normpath(base.as_posix()).replace("\\", "/")
        if norm.startswith(".."):
            return None
        candidates = [norm]
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
            candidates.append(norm + ext)
            candidates.append(f"{norm}/index{ext}")
        return next((c for c in candidates if c in fileset), None)

    for p in paths:
        r = rel[p]
        text = _read(p)
        if not text:
            continue
        ext = p.suffix

        if ext == ".py":
            pkg_parts = list(Path(r).parent.parts)
            for m in _PY_FROM.finditer(text):
                dots, mod, names = m.group(1), m.group(2), m.group(3)
                names_list = [n.strip().split(" as ")[0].strip("() \\") for n in names.split(",")]
                names_list = [n for n in names_list if n and n != "*"]
                if dots:
                    up = len(dots) - 1
                    base = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else []
                    mod_parts = base + ([x for x in mod.split(".") if x] if mod else [])
                    cands = [".".join(mod_parts)] + [".".join(mod_parts + [n]) for n in names_list]
                    for c in cands:
                        if not c:
                            continue
                        for hit in py_index.get(c, ()):
                            # relative: require exact path match, not just a suffix
                            hp = Path(hit).with_suffix("").parts
                            hp = hp[:-1] if hp and hp[-1] == "__init__" else hp
                            if list(hp) == c.split("."):
                                imports[r].add(hit)
                else:
                    cands = [mod] + [f"{mod}.{n}" for n in names_list] if mod else names_list
                    for c in cands:
                        imports[r].update(py_index.get(c, ()))
            for m in _PY_IMPORT.finditer(text):
                for name in m.group(1).split(","):
                    name = name.strip().split(" as ")[0].strip()
                    imports[r].update(py_index.get(name, ()))
        elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs"):
            for m in _JS_REL.finditer(text):
                target = js_resolve(r, m.group(1))
                if target:
                    imports[r].add(target)
        elif ext in (".java", ".kt"):
            for token in set(_JVM_TOKEN.findall(text)):
                for hit in jvm_index.get(token, ()):
                    imports[r].add(hit)

    for r in files:
        imports[r].discard(r)
    return imports, files


def _task_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text))
    out = set()
    for t in tokens:
        low = t.lower()
        if low in _STOPWORDS:
            continue
        out.add(low)
    return out


def find_seeds(files: list[str], analysis: TaskAnalysis) -> list[str]:
    # Generic areas (api/ui/user) have broad path hints like "controller"; when a
    # specific area (payment, auth, ...) is present they would only add noise.
    generic = {Area.API, Area.UI, Area.USER}
    specific = [a for a in analysis.areas if a not in generic]
    hints: set[str] = set()
    for area in specific or analysis.areas:
        hints.update(AREA_PATH_HINTS.get(area, []))
    hints.update(_task_tokens(analysis.text))

    # A file named explicitly in the task wins over fuzzy matching.
    explicit = set(re.findall(r"[\w./-]+\.(?:py|js|jsx|ts|tsx|java|kt|mjs)", analysis.text))
    named = [f for f in files if any(f == e or f.endswith("/" + e) or Path(f).name == Path(e).name for e in explicit)]
    if named:
        return named

    # Match against the path without its extension ("java" must not match every .java file).
    return [f for f in files if any(h in str(Path(f).with_suffix("")).lower() for h in hints if h)]


def analyze_impact(root: Path, analysis: TaskAnalysis) -> ImpactReport:
    imports, files = build_graph(root)
    report = ImpactReport(files_scanned=len(files))
    if not files:
        report.notes.append("분석 가능한 소스 파일(py/js/ts/java/kt)을 찾지 못했습니다.")
        return report

    seeds = find_seeds(files, analysis)
    if not seeds:
        report.notes.append("작업 설명과 연결되는 파일을 찾지 못했습니다. 영향 범위는 알 수 없음으로 처리됩니다.")
        return report

    dependents: dict[str, set[str]] = {f: set() for f in files}
    for src, targets in imports.items():
        for t in targets:
            dependents[t].add(src)

    depth: dict[str, int] = {}
    seedset = set(seeds)
    queue = deque((s, 0) for s in seeds)
    visited = set(seeds)
    while queue:
        node, d = queue.popleft()
        for dep in dependents.get(node, ()):
            if dep not in visited:
                visited.add(dep)
                depth[dep] = d + 1
                queue.append((dep, d + 1))

    indirect = sorted(f for f in visited if f not in seedset)
    report.direct_files = sorted(seedset)
    report.indirect_files = indirect
    report.indirect_depth = {f: depth[f] for f in indirect}

    touched = [f.lower() for f in visited]
    report.security_impact = any(h in f for f in touched for h in SECURITY_PATH_HINTS)
    report.database_impact = any(h in f for f in touched for h in DATABASE_PATH_HINTS)
    report.notes.append("정규식 기반 휴리스틱 분석입니다(타입/호출 그래프 해석 없음).")
    return report
