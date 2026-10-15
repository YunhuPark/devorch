"""`devorch doctor` — why can't DevOrch do its job on this machine? Local checks only: no network call, no LLM call, and a
key's value is NEVER printed (only its length and what is wrong with it)."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from devorch.config import LEVELS, devorch_dir, load_config, load_policy
from devorch.core.execution.sandbox import load_sandbox
from devorch.core.execution.test_runner import detect_test_command, resolve_executable
from devorch.git.manager import GitError, GitManager
from devorch.providers.factory import CLASSES, build_registry, key_problem
from devorch.security.command_policy import check_command, split_command

MIN_PLAUSIBLE_KEY_LEN = 20  # every vendor's real keys are far longer; a handful of characters means a failed paste


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str
    hint: str = ""


def _set_key_hint(env_name: str) -> str:
    if os.name == "nt":
        return (
            f"키를 복사한 뒤 PowerShell 에서: $env:{env_name} = (Get-Clipboard -Raw).Trim()   "
            "(입력창에 Ctrl+V 로 붙여넣지 마세요 — 제어문자 하나만 들어갈 수 있습니다)"
        )
    return f"키를 이 셸에서만: export {env_name}='<키>'   (셸 기록에 남지 않게 주의)"


def run_checks(root: Path, env: Mapping[str, str] | None = None) -> list[Check]:
    env = os.environ if env is None else env
    root = Path(root).resolve()
    out: list[Check] = []

    v = sys.version_info
    out.append(Check("Python", "ok" if v >= (3, 11) else "fail", f"{v.major}.{v.minor}.{v.micro}", "" if v >= (3, 11) else "Python 3.11 이상이 필요합니다."))

    git_exe = shutil.which("git")
    out.append(Check("git", "ok" if git_exe else "warn", git_exe or "git 을 찾을 수 없음", "" if git_exe else "git 을 설치하세요. 작업 트리/커밋 범위 리뷰와 브랜치 커밋에 필요합니다."))
    try:
        GitManager(root).ensure_ready()
        out.append(Check("git 저장소", "ok", f"{root} (최상위, 커밋 있음)"))
    except GitError as e:
        out.append(Check("git 저장소", "warn", str(e), "`devorch review --diff-file`은 저장소 없이도 되지만, 작업 트리 리뷰·`--apply`·MCP 적용에는 최상위 저장소와 첫 커밋이 필요합니다."))
    except Exception as e:  # noqa: BLE001 — a broken git must not break the diagnosis
        out.append(Check("git 저장소", "warn", f"확인하지 못함: {type(e).__name__}: {e}"))

    d = devorch_dir(root)
    if (d / "config.yaml").exists() and (d / "policy.yaml").exists():
        out.append(Check("프로젝트 설정", "ok", str(d)))
    else:
        out.append(Check("프로젝트 설정", "warn", f"{d} 에 config.yaml / policy.yaml 이 없음(기본값으로 동작)", f'devorch init -p "{root}"'))
    try:
        config, policy = load_config(root), load_policy(root)
    except (ValueError, yaml.YAMLError, OSError) as e:
        out.append(Check("설정 파일", "fail", f"읽지 못함: {str(e).splitlines()[0] if str(e) else type(e).__name__}", "YAML 문법과 값을 고친 뒤 다시 실행하세요."))
        return out

    registry, skipped = build_registry(config, env)
    provs = config.get("providers") or {}
    for name in CLASSES:
        cfg = provs.get(name) or {}
        key_env = cfg.get("api_key_env")
        raw = (env.get(key_env) or "") if key_env else ""
        if name in registry.names():
            p = registry.get(name)
            priced = bool(getattr(p, "prices", None))
            short = len(raw.strip()) < MIN_PLAUSIBLE_KEY_LEN and key_env
            detail = f"사용 가능 · model={getattr(p, 'model', '?')} · " + ("단가 설정됨" if priced else "단가 없음(비용이 unknown, 비용 상한 사용 불가)")
            if short:
                detail += f" · 키 길이 {len(raw.strip())}: 너무 짧음, 올바른 키인지 확인"
            ok = priced and not short
            hint = ""
            if not priced:
                hint = f"config.yaml 의 providers.{name}.input_per_mtok / output_per_mtok 를 공급사 가격표대로 적으세요(100만 토큰당 USD)."
            out.append(Check(f"provider {name}", "ok" if ok else "warn", detail, hint))
        else:
            reason = skipped.get(name, "알 수 없음")
            if raw and (problem := key_problem(raw.strip())):
                reason = f"환경변수 {key_env}: 길이 {len(raw)}, {problem}"
            hint = ""
            if key_env and (not raw.strip() or key_problem(raw.strip())):
                hint = _set_key_hint(key_env)
            elif "model" in reason:
                hint = f"config.yaml 의 providers.{name}.model 에 공급사 문서에서 확인한 모델 ID를 적으세요(추측하지 않습니다)."
            elif "SDK" in reason:
                hint = 'pip install -e ".[llm]"'
            out.append(Check(f"provider {name}", "warn", reason, hint))

    out.append(
        Check("사용 가능한 provider", "ok", ", ".join(registry.names()))
        if registry.names()
        else Check("사용 가능한 provider", "fail", "하나도 없음 — 어떤 리뷰도 실행할 수 없습니다", "위 provider 항목의 안내대로 키/모델을 설정하세요. 시연만 하려면 `devorch run ... --mock`")
    )

    referenced: set[str] = set()
    for lvl in LEVELS:
        roles = policy.rules[lvl]
        reviewers = {r: p for r, p in roles.items() if r != "implementation"}
        referenced |= set(roles.values())
        need = sorted(set(reviewers.values()))
        if not need:
            out.append(Check(f"위험도 {lvl}", "ok", "외부 리뷰어 불필요"))
            continue
        missing = [p for p in need if p not in registry.names()]
        who = ", ".join(f"{r}={p}" for r, p in reviewers.items())
        out.append(
            Check(f"위험도 {lvl}", "ok", f"리뷰어 {who} — 모두 사용 가능")
            if not missing
            else Check(f"위험도 {lvl}", "warn", f"리뷰어 {who} — 부족: {', '.join(missing)} (이 위험도의 리뷰는 시작 전에 거부됨)", "provider 설정 또는 policy.yaml 의 rules 를 조정하세요.")
        )

    cap = policy.limits.max_cost_usd
    if cap is None:
        out.append(Check("비용 상한", "info", "limits.max_cost_usd 가 없음(상한 없음)", "비용이 걱정되면 policy.yaml 에 limits.max_cost_usd 를 정하세요(모든 사용 provider에 단가 필요)."))
    else:
        unpriced = registry.unpriced(referenced & set(registry.names()))
        out.append(
            Check("비용 상한", "ok", f"작업당 ${cap} (호출 사이에 검사, 병렬 호출분은 넘을 수 있음)" + ("" if registry.names() else " — 사용 가능한 provider가 없어 단가는 확인하지 못함"))
            if not unpriced
            else Check("비용 상한", "fail", f"상한 ${cap} 이 설정됐지만 단가가 없는 provider가 있어 지킬 수 없음: {', '.join(unpriced)} — 실행이 거부됩니다", "해당 provider 의 input_per_mtok / output_per_mtok 를 설정하거나 상한을 null 로 두세요.")
        )

    cmd = (config.get("project") or {}).get("test_command") or detect_test_command(root)
    if not cmd:
        out.append(Check("테스트 명령", "warn", "자동 감지하지 못함", "config.yaml 의 project.test_command 를 정하세요(테스트가 없으면 --no-tests)."))
    else:
        allowed, why = check_command(cmd, policy.allowed_commands)
        if not allowed:
            out.append(Check("테스트 명령", "warn", f"{cmd} — {why}", "policy.yaml 의 allowed_commands 에 이 명령을 추가해야 테스트 단계가 실행됩니다."))
        else:
            try:
                prog = split_command(cmd)[0]
            except ValueError:
                prog = ""
            found = resolve_executable(prog, root, env) if os.name == "nt" else shutil.which(prog)
            out.append(
                Check("테스트 명령", "ok", f"{cmd}" + (f"  (실행 파일: {found})" if found and os.name == "nt" else ""))
                if found
                else Check("테스트 명령", "warn", f"{cmd} — 실행 파일 {prog!r} 을(를) PATH 에서 찾을 수 없음", "해당 도구를 설치하거나 PATH 에 추가하세요(테스트 단계가 실패합니다).")
            )

    sb = load_sandbox(config)
    if not sb.enabled:
        out.append(Check("샌드박스", "info", "mode=none — 테스트가 이 컴퓨터에서 직접 실행됩니다(AI가 쓴 코드가 호스트에서 실행됨). 악의적이거나 신뢰할 수 없는 에이전트라면 그 테스트가 서명 키를 읽어 스스로 승인할 수 있습니다(재현됨)", "신뢰할 수 없는 에이전트와 쓰려면 config.yaml 의 project.sandbox 로 docker 를 설정하세요(docker 에서는 같은 시도가 막힘을 확인)."))
    elif not shutil.which("docker"):
        out.append(Check("샌드박스", "fail", "mode=docker 인데 docker 를 찾을 수 없음 — 테스트가 실패합니다(호스트 실행으로 대체하지 않음)", "docker 를 설치하거나 mode 를 none 으로 바꾸세요."))
    elif not sb.image:
        out.append(Check("샌드박스", "fail", "mode=docker 인데 image 가 없음", "project.sandbox.image 를 정하세요(테스트 의존성이 든 이미지)."))
    else:
        out.append(Check("샌드박스", "ok", f"docker image={sb.image} (daemon 상태는 `devorch sandbox` 로 확인)"))

    launcher = "devorch" if shutil.which("devorch") else f'"{sys.executable}" -m devorch'
    out.append(Check("MCP 등록", "info", f'claude mcp add devorch -- {launcher} mcp -p "{root}"', "연결 시험만 하려면 끝에 --mock 을 붙이세요. `devorch` 명령이 안 되면 `python -m devorch` 로 실행합니다."))
    return out


_MARK = {"ok": "[OK]", "warn": "[주의]", "fail": "[실패]", "info": "[정보]"}


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{_MARK[c.status]:<6} {c.name}: {c.detail}")
        if c.hint and c.status != "ok":
            lines.append(f"       → {c.hint}")
    n = {s: sum(c.status == s for c in checks) for s in _MARK}
    lines.append(f"\n실패 {n['fail']} · 주의 {n['warn']} · 정상 {n['ok']}")
    return "\n".join(lines)
