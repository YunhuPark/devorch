# DevOrch — AI Development Control Plane

AI에게 코드를 맡겨도 되는지 **판단하고 통제**하는 엔진.
여러 LLM을 연결하는 도구가 아니라, 작업의 위험도·영향 범위를 분석해 모델을 배정하고, 독립 검증 → 사람 승인 → 감사 기록까지 관리합니다.

```
Task → Analyze → Risk → Impact → Route → Implement → Independent Review → Evaluate → Human Approval → Apply/Test → Audit
```

## 빠른 시작

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,llm]"

devorch init                                   # .devorch/config.yaml, policy.yaml 생성
devorch analyze "로그인 기능에 JWT 인증 추가해줘"   # LLM 호출 없이 위험도/영향/라우팅 출력
devorch analyze "결제 API 수정" --json
devorch run "로그인 JWT 인증 추가" --mock         # 가짜 Provider로 전체 워크플로 시연
devorch providers                              # 어떤 provider가 사용 가능한지(키/모델 설정) 확인
pytest
```

## 현재 구현 상태

| 구성요소 | 상태 |
|---|---|
| Task Analyzer / Risk Engine (규칙 기반 점수 + 최소 등급 floor) | ✅ |
| Change Impact Analyzer (py/js/ts/java/kt import·참조 그래프, 정규식 휴리스틱) | ✅ |
| Model Router + `policy.yaml` (등급별 역할→모델, 승인 규칙, critical 독립 리뷰어 수) | ✅ |
| Evaluator (리뷰 파싱, 중복 병합, READY / WARNINGS / NOT_READY) | ✅ |
| 상태 머신 + 워크플로 엔진 (독립 리뷰 병렬, 수정 라운드 `max_retries`, 승인 게이트) | ✅ |
| Audit log (JSONL, 시크릿 마스킹), 명령 allowlist, 경로 탈출 방지 | ✅ |
| Provider 인터페이스 + MockProvider | ✅ |
| 실제 LLM Provider 어댑터 (Anthropic Messages / OpenAI Responses / Gemini generate_content) | ✅ 단위 테스트 완료. **Anthropic만** 실제 호출 성공을 확인(벤치마크 11회) — **OpenAI·Gemini는 실제 호출 미검증** |
| 코드 적용기(임시 git worktree) / 테스트 러너(allowlist) / 브랜치 커밋 — `run --apply` | ✅ 임시 git 저장소 E2E 테스트 완료 (실제 LLM이 만든 diff로는 미검증) |
| MCP 서버 (stdio, 도구 8개 + 리소스 3개), `devorch approve` / `tasks` | ✅ 인메모리 테스트 + **Windows에서 실제 stdio 하위 프로세스로** 분석→리뷰→테스트→브랜치 커밋, 승인 필요 작업의 거부까지 검증(`tests/test_mcp_stdio.py`, mock provider). **Claude Code 등 실제 클라이언트 연결은 미검증** |
| Benchmark (`devorch bench`): 시드 결함 11케이스, Wilson 신뢰구간, 쌍체 부호검정 | ✅ 하네스·채점은 mock으로 검증. 실제 LLM은 **Claude 단일 리뷰어(`--claude-only`) 1회 실행만** 있음 — 표본이 작아 결론을 내릴 수 없고, GPT·Gemini 교차 검증과 single 대 multi 비교는 **미실행** |
| Docker 샌드박스 (테스트 단계, `devorch sandbox`) | ✅ 가짜 docker 실행파일로 인자·타임아웃·fail-closed 검증 — **실제 컨테이너 실행은 이 환경에 daemon이 없어 미검증** |
| Dashboard (`devorch dashboard`, 읽기 전용 로컬 웹 UI) | ✅ API 테스트 + 실제 Chromium 렌더링/XSS 확인 |
| Cost Optimizer, Adaptive Routing | ⏳ 고도화 (실측 비용·품질 데이터가 먼저 필요) |

`--apply` 없이 실행하면 아무것도 적용하지 않고, 승인된 diff 제안만 `.devorch/runs/<id>.diff`에 저장합니다.
`--apply`를 주면 승인 **이후** 다음을 수행합니다.

```
diff 추출 → 경로 검증 → git worktree add(HEAD 기준) → git apply → 테스트 → 브랜치에 커밋
```

사용자의 현재 브랜치·작업 트리는 건드리지 않고 `devorch/<run>-<slug>` 브랜치만 만듭니다(merge는 직접).
테스트가 실패하면 커밋하지 않고 확인용 worktree를 남깁니다. 테스트 명령이 없으면 실패하며(`--no-tests`로 명시적 생략),
테스트 명령은 `config.yaml`의 `project.test_command` 또는 자동 감지(npm/pytest/mvn/gradle)를 씁니다.

## 설계상 보장하는 것 (fail-closed)

- 리뷰 호출이 실패하거나 응답을 파싱할 수 없으면 해당 리뷰는 **통과가 아니라 미완료**로 처리 → NOT_READY
- 사람 승인이 필요한데 approver가 없으면 실패
- 계획에 필요한 provider가 등록되지 않았으면 토큰을 쓰기 전에 실패
- 리뷰어끼리는 서로의 결과를 보지 못함(독립 검증). 수정 라운드에는 *차단* 이슈만 구현 모델에 전달
- 비용/시간은 `policy.estimates`의 **기본 추정치**이며 실측값이 아님. Provider가 비용을 알려주지 않으면 `unknown`으로 표시
- **작업당 비용 상한** `limits.max_cost_usd`(기본 null=무제한): 누적 비용이 상한에 도달하면 다음 LLM 호출을 시작하지 않고(`budget_exceeded` 감사 기록)
  리뷰는 미완료(NOT_READY), 구현 단계면 작업이 FAILED가 됩니다. 상한을 지키려면 쓰는 provider 모두에 `input_per_mtok`/`output_per_mtok`가
  있어야 하며, 없으면 토큰을 쓰기 전에 시작을 거부합니다. 모든 LLM 호출은 토큰 수·호출 비용·누적 비용·상한을 감사 로그에 남깁니다.
  **한계:** 상한은 호출 *사이*에 검사하므로, 이미 진행 중인 호출(리뷰어는 병렬)만큼은 넘을 수 있습니다(넘으면 run notes에 기록). `bench`에서는 이 상한이 케이스(실행)마다 따로 적용되고, 한 케이스의 리뷰어는 동시에 출발하므로 **벤치마크 전체 지출은 막지 못합니다.**
  전체 지출은 `devorch bench run --max-total-usd <금액>`으로 제한하세요(합계가 상한에 닿으면 새 케이스를 시작하지 않고 해당 케이스는 불완전으로 표시. 진행 중인 케이스는 끝나므로 동시성(기본 4)만큼 넘을 수 있음. 모든 provider 단가 필요).

## 레이아웃

```
src/devorch/
├── core/{analyzer,router,evaluator,orchestrator,policy}/
├── core/execution/ # patch(diff 검증), executor, test_runner
├── git/            # worktree / apply / commit 래퍼
├── providers/      # base, adapters(Anthropic/OpenAI/Gemini), factory, mock
├── security/       # secret_redactor, command_policy, path_guard
├── service.py  mcp_server.py  store.py   # MCP 서비스 계층 / 서버 / 작업 영속화
├── cli/main.py
├── config.py  audit.py  models.py
bench/          # (src/devorch/bench) 케이스·채점·러너·리포트, cases/*.yaml
tests/              # 224 tests + 1 skip (Windows에서 symlink 권한이 없으면 skip)
examples/demo-spring/   # 영향 분석 시연용 작은 Java 프로젝트
```

## 실제 LLM 연결

`.devorch/config.yaml`의 `providers`에서 `api_key_env`(환경변수 이름)와 `model`을 설정합니다.
Claude는 기본 모델이 있고, **OpenAI/Gemini는 모델 ID를 직접 지정해야 합니다**(추측하지 않음).
비용은 `input_per_mtok` / `output_per_mtok`를 적은 경우에만 계산되고, 없으면 `unknown`입니다.

```yaml
providers:
  openai: {api_key_env: OPENAI_API_KEY, model: <모델 ID>, input_per_mtok: 0.0, output_per_mtok: 0.0}
```

`devorch run`(--mock 없이)은 호출 전에 계획과 예상 비용을 보여주고 확인을 받습니다.
Gemini 어댑터는 `generate_content`를 사용합니다(SDK의 Interactions API는 응답 구조를 검증하지 못해 미사용).

## `--apply` 사용 시 알아둘 점

- **기본값에서 테스트는 샌드박스가 아닙니다**(아래 Docker 샌드박스로 격리 가능). AI가 쓴 코드가 이 컴퓨터에서 실행됩니다. 보호 장치는 allowlist 명령만 허용, 셸 미사용,
  timeout, API 키/토큰류 환경변수 제거, 보호 경로(`.git/`, `.devorch/`)·바이너리·심볼릭 링크·`..` 경로 diff 거부까지입니다.
  네트워크/파일 접근은 막지 못합니다 (Docker sandbox는 이후 단계).
- AI는 **HEAD 기준**으로 작업합니다. 커밋되지 않은 변경은 보이지 않고 반영되지도 않습니다.
- credential 형태의 문자열이 있는 파일은 모델에 보내지 않습니다.
- 프로젝트 루트가 git 저장소 최상위이고 커밋이 1개 이상 있어야 합니다.

## MCP로 Claude Code 등에 연결

```bash
devorch mcp -p /path/to/project            # stdio 서버 (연결 테스트: --mock)
# Claude Code 등록 예 (일반적인 형태이며 정확한 옵션은 `claude mcp add --help` 로 확인):
claude mcp add devorch -- devorch mcp -p /path/to/project
```

에이전트(Claude)가 따르는 흐름 — 코드는 **에이전트가 자기 작업 트리에서 작성**하고, DevOrch는 무엇이 반영될 수 있는지만 통제합니다.

| 단계 | 도구 | 하는 일 |
|---|---|---|
| 1 | `devorch_analyze_task` | task_id 발급, 위험도·영향·필요한 리뷰/테스트/승인 안내 |
| 2 | (에이전트가 구현) | 작업 트리 수정, 커밋 금지 |
| 3 | `devorch_request_review` | 현재 diff를 독립 리뷰어가 검토 → READY / WARNINGS / NOT_READY |
| 4 | `devorch_run_tests` | 임시 worktree에 diff 적용 후 테스트 (NOT_READY면 실행 거부) |
| 5 | `devorch_apply_changes` | 리뷰+테스트(+사람 승인)를 통과한 diff를 **새 브랜치**에 커밋 |

보조: `devorch_inspect_project`, `devorch_analyze_risk`, `devorch_analyze_impact`, `devorch_get_diff`.
리소스: `devorch://policy`, `devorch://project`, `devorch://task/{id}`.

**게이트는 모두 diff의 SHA에 묶입니다.** 리뷰·테스트·승인 뒤에 diff가 조금이라도 바뀌면 이전 결과는 무효가 되고,
검토된 것과 다른 내용은 커밋될 수 없습니다. 커밋되는 것은 작업 트리가 아니라 *검토된 diff 스냅샷*입니다.

### 사람 승인

HIGH/CRITICAL 등 승인이 필요한 작업은 `apply_changes`가 `approval_required`를 돌려줍니다.
사용자가 **터미널에서 직접** `devorch approve <task_id>` 를 실행해야 합니다(비대화형 실행은 거부, `--diff`로 전체 diff 확인).
`devorch tasks` 로 모든 작업의 게이트 상태를 봅니다. 에이전트용 승인 도구는 존재하지 않습니다.
MCP elicitation은 에이전트가 자동 응답할 수 있어 승인 수단으로 쓰지 않습니다.

> **한계(중요):** 이 승인 게이트는 *MCP 도구 경로*를 통제합니다. 같은 계정에서 셸을 실행할 수 있는 에이전트가
> `.devorch/tasks/*.approval.json`을 직접 쓰는 것까지 막지는 못합니다. 셸 권한이 있는 에이전트에 대한 방어는
> 별도의 OS 사용자/컨테이너 격리(이후 Sandbox 단계)가 필요합니다.

### 외부 LLM에 보내지 않는 것

`.env`, `*.pem`, `*.key`, `id_rsa*`, `.devorch/`는 diff 수집에서 제외되고, credential 형태의 문자열이 diff에 있으면
리뷰 자체를 거부합니다. 바이너리·심볼릭 링크·`..`/절대 경로·`.git/` 변경도 거부합니다. diff는 200KB 한도입니다.
리뷰는 작업당 최대 `limits.max_retries + 1`회이며, 동일 diff의 재요청은 캐시되어 비용이 들지 않습니다.

## Benchmark — 리뷰어가 심어둔 결함을 찾는가

```bash
devorch bench cases                              # 케이스/결함 목록 (결함 9개 + 오탐 측정용 대조군 3개)
devorch bench run --mock                         # 하네스 점검용 (가짜 리뷰어)
devorch bench run --mode both --repeats 3        # 실제 LLM: 호출 횟수 확인 후 진행, .devorch/bench/<시각>/ 에 report.md, results.json
```

- `single`(범용 리뷰어 1명) vs `multi`(policy의 HIGH 등급 전문 리뷰어 + 범용)를 같은 diff로 비교합니다.
- 지표: 탐지율, 대조군 오탐률(MEDIUM 이상 지적), 불완전 리뷰 수, 매칭 안 된 지적 수, 비용(단가 설정 시), 지연. 95% Wilson 구간과 정확 부호검정을 함께 보고합니다.
- **`--mock` 결과에는 항상 경고 배너가 붙으며 실제 성능이 아닙니다.**
- 리포트 맨 위에 불완전 리뷰(호출/파싱 실패)가 있으면 경고와 실패 사유가 나오고, 전부 실패하면 "성능 수치가 아님"이라고 표시합니다. 사용한 모델 ID도 기록됩니다.
- 호출별 입력/출력 **토큰 수는 단가가 없어도 기록**됩니다(`results.json`의 `usage`, 리포트의 토큰 열). 단가를 나중에 알게 되면 토큰 × 단가로 비용을 계산할 수 있습니다. 호출이 실패한 경우는 토큰을 세지 않습니다.
- 실측은 아직 Claude 단일 리뷰어 1회뿐입니다(탐지 9/9, 대조군 오탐 1/3). 구간이 넓습니다. 그 오탐 1건은 대조군 `c11`의 과제 문구가 모호해서 정당한 지적일 수 있었고,
  그래서 `c11`의 문구를 개정했습니다(2026-10-08, 케이스 파일의 notes 참고). **개정 전 실행과 오탐률을 직접 비교하지 마세요.**
- 한계: 케이스가 적고, 탐지 판정이 정규식이며, LLM 출력이 비결정적입니다. 자체 케이스를 `--cases-dir`로 추가할 수 있습니다.

## Docker 샌드박스 (테스트 단계)

`.devorch/config.yaml`:

```yaml
project:
  sandbox: {mode: docker, image: <테스트 의존성이 이미 들어 있는 이미지>}   # network: none(기본)|bridge, memory, cpus, pids
```

`devorch sandbox` 로 docker/daemon/이미지 상태를 점검합니다(이미지 안의 코드는 실행하지 않음).
컨테이너에는 임시 worktree만 `/work`로 마운트되고, 네트워크 없음·호스트 환경변수 없음·모든 capability 제거·
`no-new-privileges`·비root·메모리/CPU/pid 제한이 적용됩니다. 타임아웃 시 컨테이너를 `docker rm -f`로 정리합니다.

- **fail-closed:** docker가 없거나 이미지가 없거나 컨테이너를 시작하지 못하면 테스트는 *실패*합니다. 호스트 실행으로 대체하지 않습니다.
- 네트워크가 없으므로 `npm install` 등은 불가 — 의존성은 이미지에 미리 넣어야 합니다. 이미지는 직접 지정해야 합니다(추측 안 함).
- 컨테이너는 VM이 아닙니다(커널/Docker 탈출 취약점은 막지 못함). `network: bridge`를 켜면 코드가 네트워크에 접근할 수 있습니다.
- 검증 범위: 인자 구성·타임아웃·오류 처리는 가짜 `docker` 실행파일로 테스트했습니다. **실제 컨테이너에서의 실행은 아직 해보지 못했습니다.**

## Dashboard

```bash
devorch dashboard -p /path/to/project     # http://127.0.0.1:8765 (--port 0 = 자동 선택)
```

작업 목록(위험도·리뷰/테스트/승인 게이트), 작업 상세(위험 요인·리뷰 결과·diff), 감사 로그(작업별 필터), 벤치마크 보고서를 봅니다.

- **읽기 전용:** GET 외 요청은 405, 승인/쓰기 엔드포인트가 없습니다. 승인은 터미널의 `devorch approve`로만 합니다.
- loopback 바인딩, Host 헤더 검사(DNS rebinding 방지), CORS 없음, 원격 출처 없는 CSP. LLM이 만든 텍스트는 `textContent`로만 삽입합니다.
- **인증이 없습니다.** diff와 감사 로그가 보이므로 `--host`로 외부에 노출하지 마세요(노출 시 경고를 띄움).
