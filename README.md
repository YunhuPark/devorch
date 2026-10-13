# DevOrch — AI Development Control Plane

AI에게 코드를 맡겨도 되는지 **판단하고 통제**하는 엔진.
여러 LLM을 연결하는 도구가 아니라, 작업의 위험도·영향 범위를 분석해 모델을 배정하고, 독립 검증 → 사람 승인 → 감사 기록까지 관리합니다.

```
Task → Analyze → Risk → Impact → Route → Implement → Independent Review → Evaluate → Human Approval → Apply/Test → Audit
```

## 빠른 시작

**Windows (PowerShell)** — Python 3.11+ 와 git 이 필요합니다.
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # 실행 정책 오류가 나면: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".[dev,llm]"
```
**macOS / Linux**
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,llm]"
```
설치가 `No such file or directory: ...\anthropic\types\...` 같은 오류로 실패하면 Windows 경로 길이 제한 때문입니다(2026-10-12 깊은 폴더에서 재현, `C:\dv\venv` 처럼
짧은 경로의 venv 로는 성공). 짧은 경로에 venv 를 만들거나 [Windows 긴 경로 지원](https://pip.pypa.io/warnings/enable-long-paths)을 켜세요.

그다음 (공통):
```
devorch doctor                                 # 이 컴퓨터에서 막히는 곳을 점검하고 해결 방법을 알려줌 (네트워크·LLM 호출 없음, 키 값은 출력 안 함)
devorch init                                   # .devorch/config.yaml, policy.yaml 생성
devorch usage                                  # 감사 로그에서 작업별·provider별·날짜별 실제 지출 집계 (--days 7, --json). 단가 없는 호출은 $0이 아니라 '알 수 없음'으로 표시
devorch analyze "로그인 기능에 JWT 인증 추가해줘"   # LLM 호출 없이 위험도/영향/라우팅/예상 비용 출력
devorch run "로그인 JWT 인증 추가" --mock         # 가짜 Provider로 전체 워크플로 시연
devorch review --git-range HEAD~1..HEAD --task "로그인 JWT 인증 추가"   # 정책이 정한 독립 리뷰어에게 실제 변경을 리뷰받음
pytest
```
- `devorch` 명령이 실행되지 않으면(런처 문제) `python -m devorch ...` 로 똑같이 실행됩니다.
- **API 키는 환경변수로만** 넣습니다. PowerShell: 키를 복사한 뒤 `$env:OPENAI_API_KEY = (Get-Clipboard -Raw).Trim()`. 입력창에 `Ctrl+V` 로 붙여넣는 방식(`Read-Host -AsSecureString`)은 이 환경에서 제어문자 하나만 들어가 실패했습니다.
  OpenAI·Gemini 는 `config.yaml` 에 **모델 ID와 단가(100만 토큰당 USD)** 도 직접 적어야 합니다(추측하지 않습니다). 상태는 `devorch doctor` 로 확인하세요.
- `devorch review`: 작업 트리(기본) / `--git-range A..B` / `--diff-file` 의 변경을 리뷰합니다. **실행 전에 예상 비용을 보여주고 확인을 받으며**, 끝나면 실제 비용과 비교합니다.
  자격증명이 있거나 너무 큰 diff, 비용 상한을 넘을 것 같은 리뷰는 호출 전에 거부합니다. 위험 수준은 `--level` 로 **높이기만** 가능합니다. `NOT_READY` 면 종료 코드 1이라 CI 게이트로 쓸 수 있습니다.

### Windows 테스트 실행에 대해
`npm`/`mvn`/`gradle` 은 Windows 에서 `.cmd`/`.bat` 실행 파일이라, 이전에는 **테스트 단계가 항상 "실행 파일을 찾을 수 없음"으로 실패**했습니다(JS·Java 프로젝트가 Windows 에서 쓸 수 없었음).
이제 `PATHEXT` 로 실행 파일을 찾되 **현재 디렉터리와 상대 경로 PATH 항목은 검색하지 않고**(AI가 worktree 에 쓴 `npm.cmd` 가 이기지 못하게), 배치 파일이 cmd.exe 를 거치므로
인자에 `%` `^` `!` `"` 가 있으면 거부합니다. `devorch doctor` 가 테스트 명령의 실행 파일을 찾을 수 있는지 알려줍니다. 실제 `npm`/`mvn` 으로는 아직 돌려 보지 못했고, 가짜 `.cmd` 로 검증했습니다(**미검증**).

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
| 실제 LLM Provider 어댑터 (Anthropic Messages / OpenAI Responses / Gemini generate_content) | ✅ 단위 테스트 완료. 실제 호출 성공 확인: Anthropic(`claude-sonnet-5-5`, 11회), OpenAI(`gpt-6-sol`)·Gemini(`gemini-3.6-flash`)(작은 벤치마크 8회). **미검증:** OpenAI 추론 토큰이 `usage.output_tokens`에 포함되는지(청구서와 대조 필요), 실패·재시도 경로의 실제 동작 |
| 코드 적용기(임시 git worktree) / 테스트 러너(allowlist) / 브랜치 커밋 — `run --apply` | ✅ 임시 git 저장소 E2E 테스트 완료 (실제 LLM이 만든 diff로는 미검증) |
| MCP 서버 (stdio, 도구 8개 + 리소스 3개), `devorch approve` / `tasks` | ✅ 인메모리 테스트 + **Windows에서 실제 stdio 하위 프로세스로** 분석→리뷰→테스트→브랜치 커밋, 승인 필요 작업의 거부까지 검증(`tests/test_mcp_stdio.py`, mock provider). **Claude Code 등 실제 클라이언트 연결은 미검증** |
| Benchmark (`devorch bench`): 시드 결함 16케이스(결함 13 + 대조군 4), Wilson 신뢰구간, 쌍체 부호검정 | ✅ 하네스·채점은 mock으로 검증. 실제 LLM 실행(2026-10-08, 당시 11케이스): Claude 단일 리뷰어 1회, GPT·Gemini single 대 multi를 3회 반복(132호출, 약 $0.49). 결함 단위 탐지 single 8/9 [56–98%] 대 multi 9/9 [70–100%], 차이는 c08 하나(쌍체 p=1.000)라 **multi가 낫다고 말할 수 없음**. 이후 아키텍처 케이스 4개와 대조군 1개를 추가(2026-10-09)하고 16케이스로 4개 모드를 1회씩 실행(96호출, $0.347): 결함 단위 탐지 multi 12/13($0.175), single 10/13($0.057), `solo:gemini:review` 12/13($0.061), `solo:openai:architecture` 12/13($0.054). 쌍체 p는 0.5~1.0이라 **통계적으로 차이가 확인되지 않음**. 정규식 판정의 한계(c14는 single이 실제로 지적했으나 미탐지로 집계), c15는 4개 모드 모두 다른 문제(TTL)를 지적해 비교에 쓸 수 없음, c16 대조군은 자리표시 데이터 때문에 완전히 깨끗하지 않음 — 각 케이스 notes 참고. LLM 판정기(Claude, 52호출 $0.131)로 재판정하면 정규식과 51/52 일치, 엇갈린 1건(c14 single, 판정기는 탐지) → single 11/13, 나머지는 동일, 쌍체 p=1.0 (판정기는 검증되지 않았고 사람이 읽은 판단과 같은 계열 모델이라 독립 확인이 아님). 비용 차이(multi가 다른 구성의 약 3배)는 안정적이나 탐지 차이는 결함 1개 |
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
- **실행 전 예상 비용**: 리뷰어 호출은 `토큰 × 단가`로 계산합니다(단가는 `config.yaml`의 `providers.<name>.input_per_mtok/output_per_mtok`).
  **입력 토큰**은 ASCII 글자와 한글 등 비ASCII 글자를 따로 세어(한글은 글자당 약 0.95토큰) 벤치마크 16케이스(맞추는 데 쓰지 않은 데이터)에서 케이스별 −5~+10%, 실제 6~36KB diff 4건에서 ±5%였습니다
  (이전의 단일 비율 식은 한글 많은 diff에서 −35%였음). **출력 토큰**(생각 토큰 포함)은 diff가 커질수록 늘고 편차가 커서(GPT 27~1,566, Gemini 249~4,096 이상) **점이 아니라 범위(낮음~높음)**로 보여줍니다.
  **실측 이력(2026-10-11, 이 저장소 자신의 커밋 리뷰)**: 처음 4건으로 범위를 정하고 *새로운* 7건(호출 14개, 호출당 최대 2만 3천 토큰 입력)에 적용했을 때 입력 토큰은 14개 모두 −2~+6%,
  출력 범위는 14개 중 12개가 안(무거운 지적이 나온 2건이 위로 벗어남), 리뷰 합계 비용은 7건 중 6건이 범위 안이었습니다. 그래서 상한을 **넓히고**(좁히지 않음) Gemini 중간값 기울기를 보정했으며,
  그 결과 **지금 모델은 실제 리뷰 11건에 대해 다시 in-sample(검증이 아님)**입니다. 중간값의 리뷰 단위 오차는 −36%~+24%였으니 **범위로 읽으세요**.
  한계: (1) 위 이유로 출력 범위는 검증된 것이 아니고, 입력 약 4만 3천 토큰(리뷰 합계)보다 큰 diff는 측정한 적이 없으며, (2) 분석 시점에는 diff가 없어 크기를 **가정**(파일당 1,500자)하고
  (실제 리뷰 직전에는 진짜 diff로 다시 계산해 감사 로그 `mcp:review_estimate`와 결과의 `estimated_review_cost_usd`/`..._range_usd`에 남김), (3) **구현 단계**는 실제 파일 내용 크기(입력)와 *가정한* 출력 범위로 계산합니다 — 실측은 작은 수정 1건(입력 265 / 출력 182 토큰)뿐이라 큰 변경의 출력은 검증되지 않았고
  (`limits.implementation_max_tokens`로 상한), **단가가 없는 provider는 여전히 기본 고정값**(`policy.estimates.cost_usd`, 실측 없음, 범위 없음)이며 MCP `analyze_task`는 구현 단계를 합계에서 뺍니다(구현은 클라이언트가 하고 DevOrch는 리뷰어만 호출), (4) claude의 토큰 계수는 측정한 적이 없어 가정값입니다. 단가가 하나도 없으면 예전처럼 고정값만 씁니다.
  **실패한 호출도 청구됩니다**: 응답이 `max_tokens`에서 잘리거나 비어 있어도 API가 보고한 토큰은 비용·사용량·감사 로그·비용 상한에 모두 반영됩니다(`failed_calls`). 단가가 없으면 `unknown`입니다.
  리뷰어 호출당 출력 한도는 `limits.review_max_tokens`(기본 8192)입니다. 생각 토큰이 한도를 쓰는 모델(Gemini)은 약 9천 토큰 diff에서 기존 4096으로는 잘렸습니다(리뷰는 미완료, 비용은 청구됨)
- **비용 상한·단가는 유한한 값만** 받습니다: `limits.max_cost_usd`, `--max-total-usd`, `providers.<name>.input_per_mtok/output_per_mtok` 에 `nan`/`inf`/음수를 넣으면 거부합니다
  (NaN 은 모든 비교가 False 라 `spent >= nan` 이 영원히 참이 되지 않아 상한이 작동하지 않았습니다 — AI 리뷰어가 지적했고 재현해서 고쳤습니다).
- **작업당 비용 상한** `limits.max_cost_usd`(기본 null=무제한): 누적 비용이 상한에 도달하면 다음 LLM 호출을 시작하지 않고(`budget_exceeded` 감사 기록)
  리뷰는 미완료(NOT_READY), 구현 단계면 작업이 FAILED가 됩니다. 상한을 지키려면 쓰는 provider 모두에 `input_per_mtok`/`output_per_mtok`가
  있어야 하며, 없으면 토큰을 쓰기 전에 시작을 거부합니다. 모든 LLM 호출은 토큰 수·호출 비용·누적 비용·상한을 감사 로그에 남깁니다.
  **한계:** 상한은 호출 *사이*에 검사하므로, 이미 진행 중인 호출(리뷰어는 병렬, 판정기는 동시성 4)만큼은 넘을 수 있습니다(넘으면 run notes에 기록). 리뷰 직전의 사전 거부는 예상의 **중간값**을 쓰므로
  실제 비용이 범위의 높은 쪽으로 가면 상한을 넘을 수 있습니다. `bench`에서는 이 상한이 케이스(실행)마다 따로 적용되고, 한 케이스의 리뷰어는 동시에 출발하므로 **벤치마크 전체 지출은 막지 못합니다.**
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
tests/              # 478 tests + 2 skip (Windows에서 symlink 권한이 없으면 skip)
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
프로젝트 루트의 `.mcp.json` 으로 등록할 수도 있습니다. 설치된 `devorch` 실행 파일이 말을 안 듣는 환경(우리 Windows 에서 겪음)을 피하려면 **venv 의 python 으로 모듈을 실행**하세요:
```json
{"mcpServers": {"devorch": {"command": "C:\\path\\to\\.venv\\Scripts\\python.exe", "args": ["-m", "devorch", "mcp", "-p", "C:\\path\\to\\project", "--mock"]}}}
```
(`--mock` 은 가짜 리뷰어라 비용이 없는 연결 시험용입니다. 실제 리뷰를 받으려면 빼세요. `devorch doctor` 의 "MCP 등록" 줄이 이 프로젝트용 명령을 만들어 줍니다.)
에이전트가 도구를 올바른 순서로 쓰게 하려면 프로젝트의 `CLAUDE.md` 에 흐름을 적어 두세요(분석 → 구현(커밋 금지) → 리뷰 → 테스트 → 적용, `approval_required` 면 멈추고 사람이 터미널에서
`devorch approve <task_id>`; 승인을 대신하지 않기; diff 가 바뀌면 리뷰·테스트를 다시). 예시는 `devorch-playground/CLAUDE.md` 를 참고하세요.
**검증 범위(2026-10-12):** 실제 Claude Code 2.1.293(`claude -p`, `--mcp-config`, 모델 `claude-sonnet-5-5`)가 연습 프로젝트의 복사본에서 `devorch` MCP 서버에 `connected` 로 붙고 도구 8개를 인식해
`CLAUDE.md` 의 순서를 따랐습니다(`--mock`, 즉 가짜 리뷰어). ① LOW 작업은 분석 → 수정 → 리뷰 → 테스트 → 적용으로 새 `devorch/...` 브랜치에 커밋되고 `main` 은 그대로였고,
② 기존 테스트가 실패하는 상태에서는 `apply_changes` 로 가지 못하고 에이전트가 그대로 보고했으며, ③ CRITICAL 작업은 `approval_required` 에서 멈춰 사람에게 `devorch approve <id>` 를 안내했습니다
(승인 파일 없음, 추가 커밋 0, 비대화형 `approve` 는 거부). **검증하지 못한 것:** IDE(앱)의 MCP 서버 승인 화면, `.mcp.json` 프로젝트 범위 서버의 승인 흐름(헤드리스에서는 설정을 직접 넘김),
사람이 승인한 뒤 에이전트가 다시 `apply_changes` 를 호출하는 뒷부분(승인은 설계상 TTY 가 필요해 자동화하지 않음), 실제 리뷰어(mock 아님)와 함께 쓰는 경우.
셸·파일 쓰기 권한이 있는 에이전트는 승인 파일을 직접 쓸 수 있다는 기존 한계는 그대로입니다(위 시험은 Bash·Write 를 막은 상태).

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
devorch bench cases                              # 케이스/결함 목록 (결함 13개 + 오탐 측정용 대조군 4개)
devorch bench run --mock                         # 하네스 점검용 (가짜 리뷰어)
devorch bench run --mode both --repeats 3        # 실제 LLM: 호출 횟수 확인 후 진행, .devorch/bench/<시각>/ 에 report.md, results.json
```

- `single`(범용 리뷰어 1명) vs `multi`(policy의 HIGH 등급 전문 리뷰어 + 범용)를 같은 diff로 비교합니다.
  `--mode solo:<provider>[:<role>]`(예: `solo:gemini`, `solo:openai:architecture`)는 리뷰어 **한 명만** 따로 돌립니다. 쉼표로 여러 모드를 함께 지정합니다
  (`--mode single,solo:gemini:review`). 모드 간 차이에는 **모델과 프롬프트 역할이 섞여** 있을 수 있어 이렇게 분리해 확인합니다.
- **LLM 판정기** `devorch bench judge <결과 디렉터리> --judge claude`: 저장된 결과를 다시 판정합니다(리뷰어는 다시 호출하지 않음). 정규식이 놓치거나 우연히 맞힌 경우를 보려는 **두 번째 의견**이며
  정규식을 대체하지 않습니다. 판정자는 과제·diff·시드한 결함 하나·번호 매긴 지적만 보고 **어느 모델이 쓴 지적인지, 정규식이 무엇인지 모릅니다**. 호출 실패·파싱 실패·비용 상한은
  탐지도 미탐지도 아닌 **"미판정"**으로 따로 셉니다. 결과(`judged.md`, `judged.json`)는 두 판정이 **엇갈린 항목을 판정기의 사유와 함께** 나열하니 사람이 확인하세요.
  판정기 자체는 **검증되지 않았고**, 리뷰어와 같은 회사 모델이면 자기 선호 편향이 있을 수 있어 경고합니다. `--max-total-usd`로 판정 비용을 제한할 수 있습니다.
- **통계의 단위는 결함입니다.** 반복(`--repeats`)은 같은 케이스를 다시 돌린 것이라 독립 표본이 아니므로, 리포트는 `탐지(실행)`(반복을 독립으로 가정해 구간이 좁음)과
  `탐지(결함 단위)`(과반의 반복에서 탐지돼야 탐지, 불안정한 결함 수 표시)를 함께 보여주고, 쌍체 부호검정은 결함 단위로 합니다.
- 지표: 탐지율, 대조군 오탐률, 불완전 리뷰 수, 매칭 안 된 지적 수, 토큰과 비용(단가 설정 시), 지연. 95% Wilson 구간과 정확 부호검정을 함께 보고합니다.
- 오탐률은 두 가지를 **함께** 보고합니다. **엄격**: 대조군에서 MEDIUM 이상 지적이 하나라도 있으면 오탐(정당한 사소한 지적도 포함). **함정**: 케이스에 사전 선언된 함정(`traps`, 정규식)을
  MEDIUM 이상으로 지적한 경우만(예: 파라미터 바인딩을 쓴 안전한 쿼리를 SQL 인젝션이라 주장). 함정은 현재 `c11`에만 있어 표본이 매우 작고 정규식은 휴리스틱입니다.
  함정 기준은 **풀 실행 전(2026-10-08)에 정했지만, 작은 시험에서 엄격 기준이 정당한 지적(LIKE 와일드카드)까지 오탐으로 센 것을 본 뒤**입니다. 기존 실측 3건(c11)에 소급 적용하면 엄격 오탐 2건, 함정 오탐 0건입니다.
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
