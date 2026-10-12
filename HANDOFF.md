# DevOrch 인수인계 (에이전트용)

## 역할
너는 DevOrch(AI Development Control Plane)를 이어받는 시니어 백엔드/플랫폼 엔지니어다.
정직하고 신중하다. 검증하지 않은 것을 검증했다고 말하지 않는다. "되는 경로"보다 "안 되어야 하는 경로"를 먼저 의심한다.
한국어로, 결론부터, 짧게 답한다. 사용자는 비전문가일 수 있으니 명령어는 그대로 복사해서 쓸 수 있게 준다.

## 프로젝트
AI에게 코드를 맡겨도 되는지 판단·통제하는 엔진. 사용자가 밝힌 동기(2026-10-08)는 **회사의 AI 비용**이다 — 단, 기획서의 정의는
"통제 중심 Control Plane"이고 비용은 그 하위 기능이라 둘의 관계("위험에 비례해서만 검증 비용을 쓴다")는 **사용자가 아직 확정하지 않았다.**
Task → Analyze → Risk → Impact → Route → Implement → Independent Review → Evaluate → Human Approval → Apply/Test → Audit
인터페이스: CLI(`devorch`, 안 되면 `python -m devorch`), MCP 서버(stdio), 읽기 전용 Dashboard. 코드는 `src/devorch`, 사용법은 README.md.
설치: `python -m venv .venv` → `.venv\Scripts\Activate.ps1` → `pip install -e ".[dev,llm]"` → `pytest`. 막히면 `devorch doctor`.

## 현재 상태 (2026-10-09, 사실만)
- **테스트: Windows 네이티브에서 448 passed, 1 skipped** (symlink 권한이 없어 1개 skip). 전체 실행 중 무작위로 1개가 실패하는 경우가 가끔 있음 —
  이 PC의 `git.exe` 실행이 간헐적으로 거부됨(WinError 5, 원인 미확정, `git/manager.py` 에 Windows 한정 재시도 완화책).
- **타임아웃 프로세스 트리 종료(2026-10-12 수정):** `taskkill /T` 는 프로세스 트리의 스냅샷을 따라 걷기 때문에, 종료 도중 새로 만들어진 자식이 살아남았다
  (계속 fork 하는 테스트로 재현: 타임아웃 뒤 자식 6개 생존, 3/3 실패). `core/execution/winjob.py`(Windows Job Object, ctypes, KILL_ON_JOB_CLOSE)로 바꾸고
  taskkill 은 대체 수단으로 남김. 수정 뒤 같은 시험 3/3 통과. 간헐 실패하던 `test_timeout_kills_child_processes_too` 도 이 경합이 원인으로 **추정**(전체 실행 1회 실패 관측, 단독 4/4 통과였음).
  한계: 프로세스 생성 직후~job 편입 직전의 아주 짧은 구간에 자식이 생기면 job 밖일 수 있음(미검증). 테스트가 남긴 백그라운드 프로세스는 이제 테스트 종료 때 같이 종료됨(job 닫힘).
- 구현됨: Task Analyzer / Risk Engine / Impact Analyzer / Model Router + policy.yaml / Evaluator(fail-closed) / 상태 머신 / Provider 어댑터
  (Anthropic, OpenAI, Gemini) / 실행기(git worktree) / MCP 서버 / Docker 샌드박스 / Benchmark(+LLM 판정기, solo 모드, 결함 단위 통계) / Dashboard /
  **작업당 비용 상한(`limits.max_cost_usd`)과 벤치마크 합계 상한(`--max-total-usd`)** / 호출별 토큰 기록 / **토큰×단가 예상 비용** /
  `devorch review`(작업 트리·git 범위·diff 파일) / `devorch doctor` / `python -m devorch`.
- **실제로 확인한 것:** Anthropic(`claude-sonnet-5-5`), OpenAI(`gpt-6-sol`), Gemini(`gemini-3.6-flash`)의 실제 호출 성공, Windows에서 실제 stdio 하위 프로세스로 MCP 전체 흐름,
  OpenAI 사용량 화면과 DevOrch 집계의 일치(요청 6회, 입력 1,734 토큰, 지출 $0.02), 단가는 Claude 공식 페이지 원문으로 확인.
- **검증하지 못한 것(검증했다고 쓰지 말 것):**
  - **Claude Code IDE 화면에서의 연결** — 실제 Claude Code 2.1.293 을 헤드리스(`claude -p --mcp-config ... --strict-mcp-config`)로 돌려 연결과 전체 흐름(LOW 커밋, 테스트 실패 시 적용 거부, CRITICAL 에서 approval_required 정지)은
    2026-10-12 에 확인함(mock 리뷰어, 복사본에서). 남은 것: IDE 의 서버 승인 UI, `.mcp.json` 프로젝트 범위 승인 흐름, 사람 승인 뒤 에이전트가 다시 적용하는 뒷부분, 실제 리뷰어와 함께. 실행 파일 위치:
    `C:\Users\qkrdb\.antigravity-ide\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe`. 이 시험은 사용자의 Claude 구독 사용량을 쓴다(실행당 환산 $0.16~0.19) — 사용자 동의 후에만.
  - 실제 Docker 컨테이너 실행(Windows의 `1000:1000` 사용자와 `C:\` 마운트 포함).
  - **큰 diff의 비용 예측(부분 검증)** — 2026-10-11 이 저장소의 실제 커밋 4건(6.6~36KB, HIGH)을 `devorch review` 로 측정: 입력 토큰 예측은 식을 고친 뒤 ±5%, 출력 토큰은 diff가 커질수록 늘고 편차가 커서
    점 추정(처음 −42%~+48% 오차)을 버리고 **범위**로 바꿨다. 그러나 그 범위는 *본 데이터에 맞춰 정한 것(in-sample)*이라 아직 검증이 아님 — **새 커밋으로 hold-out 검증 필요**(다음 할 일 1번).
    약 1만 2천 토큰보다 큰 diff, claude 토큰 계수는 여전히 미측정.
  - **구현 단계 비용(첫 실측 1건, 2026-10-12):** 실제 `devorch run`(LOW, claude, 2줄 버그 수정): 입력 265 / 출력 182 토큰 → $2/$10 단가로 약 $0.00235. 예전 고정값 $0.05 는 약 21배 높았다.
    그래서 구현 단계도 **실제 파일 내용 크기(입력) × 단가 + 가정한 출력 범위**(`IMPLEMENTATION_OUTPUT_TOKENS` 100/400/4000)로 계산하고 `limits.implementation_max_tokens`(16384)로 출력을 제한한다.
    `devorch run` 과 `devorch analyze` 모두 적용(분석 예: `$0.0044 ($0.0014~$0.0404)`, 실측 $0.00235 는 범위 안).
    **두 번째 관측(hold-out, 예상을 호출 전에 적어 둠):** "divide 함수 + ValueError + 테스트 추가" → 입력 273 / 출력 1,155 토큰, 실제 $0.0121 대 예상 $0.0045(범위 $0.0015~$0.0405).
    범위 안이지만 중간값의 **2.7배** — 중간값은 예측으로 쓸 수 없고 범위만 쓸 수 있다. 범위는 건드리지 않음(첫 관측 뒤에 정한 범위라 두 번째가 처음으로 hold-out 이었고 통과). **n=2, 범위는 여전히 가정** — 큰 변경의 출력(diff 길이)은 미검증.
    (같은 실행에서 요약 문구가 "구현 단계는 고정값"이라고 잘못 말하던 것을 발견해 수정: 이제 비용 줄의 근거에 따라 달라짐.)
    분석기가 이 작업을 TEST 유형으로 분류함("테스트 추가" 때문, 실제로는 기능 추가) — 키워드 휴리스틱의 한계, 미수정.
    MCP `analyze_task` 는 아직 구현 단계를 고정값으로 계산함(CLI 와 다름).
  - **hold-out 결과(2026-10-11, 7개 새 리뷰):** 입력 14/14 호출이 −2~+6%, 출력 범위 12/14, 리뷰 비용 범위 6/7(밖 1건은 $0.0702 대 상한 $0.0668). 상한을 넓히고 Gemini 중간값을 보정 →
    모델은 다시 in-sample. **새 hold-out 필요.** 입력 약 4만 3천 토큰(리뷰 합계)보다 큰 diff는 미측정.
  - **이 리뷰들이 우리 코드의 실제 문제를 찾았고, 검증 후 고쳤다(원칙: 지적을 믿지 말고 재현):** NaN/inf 비용 상한·`--max-total-usd`·음수 단가가 상한을 무력화(재현·수정), Windows 에서 `npm`/`mvn`/`gradle`(.cmd) 이
    아예 실행되지 않던 문제(재현·수정, 인자 `% ^ ! "` 거부, PATHEXT 검색은 cwd·상대 PATH 제외), 판정기 프롬프트 주입(구분자와 "신뢰할 수 없는 데이터" 지시), 판정 JSON 파싱을 첫 유효 객체 방식으로.
    오탐으로 판단해 테스트로 확인만 한 것: "실패한 호출의 원본 오류가 감사 로그에" — 감사 로거가 기록 시 마스킹함(테스트 추가). 문서화한 한계: 사전 상한 검사는 중간값 사용, 판정 병렬 호출은 상한을 넘을 수 있음.
    **실제 `npm`/`mvn`/`gradle` 실행은 미검증**(가짜 .cmd 로만 검증).
  - 이 측정이 드러낸 버그를 고쳤다: **실패한(잘린) 호출의 비용이 집계·상한에서 빠졌다**(Gemini 가 생각 토큰으로 max_tokens 4096 을 소진, 청구됐지만 DevOrch 는 몰랐음). 이제 `ProviderError` 가 지출을 싣고,
    `call_provider` 가 기록한다. 리뷰어 출력 한도는 `limits.review_max_tokens`(기본 8192).
  - OpenAI 추론 토큰이 `usage.output_tokens`에 포함되는지(입력·요청 수·지출은 일치, 출력 분리는 미확인). Gemini 단가는 2027년 인상 예정(요약 표 기준, 미확인).
  - LLM 판정기의 신뢰성(판정자 Claude는 내 판단과 같은 계열이라 독립 확인이 아님).
  - OpenAI/Gemini 단가·모델 ID는 공식 페이지를 *요약기*로 읽은 값(config.yaml, git 제외) — 사용 전에 원문과 대조할 것.
- **벤치마크 실측(모두 소규모, 13개 시드 결함)** — 결함 단위 탐지: single(GPT 일반) 11/13(판정기 기준, 정규식은 10/13), multi(GPT 보안+일반, Gemini 아키텍처) 12/13,
  `solo:gemini:review` 12/13, `solo:openai:architecture` 12/13. 쌍체 p=0.5~1.0이라 **탐지 차이가 확인되지 않음.** 비용은 multi $0.175 대 단일 구성 $0.054~0.061(약 3배)로 안정적.
  보안·논리 결함은 모든 구성이 찾았고, 구성 간 유일한 일관된 차이는 `c08`(계층 위반) — GPT 일반 프롬프트는 놓치고 아키텍처 프롬프트/Gemini는 찾음(프롬프트 효과가 큼).
  케이스 한계: `c15`는 4개 모드 모두 다른 문제(TTL)를 지적해 비교 불가, `c16` 대조군은 자리표시 데이터 때문에 완전히 깨끗하지 않음, `c14`는 정규식이 실제 탐지를 놓침(notes 참고).

## 절대 지킬 설계 원칙
1. fail-closed: 리뷰 실패·파싱 불가 = 통과가 아니라 "미완료"(NOT_READY). 필요한 provider가 없으면 토큰을 쓰기 전에 중단.
2. 리뷰·테스트·승인 게이트는 diff의 SHA에 묶는다. diff가 바뀌면 이전 결과는 무효.
3. 승인은 터미널 `devorch approve`(TTY 필수)로만. MCP/Dashboard에 승인·쓰기 기능을 만들지 않는다. MCP elicitation을 승인 수단으로 쓰지 않는다.
4. 사용자의 현재 브랜치·작업 트리는 건드리지 않는다. 적용은 임시 git worktree → `devorch/<run>-<slug>` 브랜치 커밋만.
5. 테스트 명령은 allowlist만, 셸 미사용, 타임아웃, API 키/토큰 환경변수 제거. docker 모드인데 docker를 못 쓰면 테스트 실패(호스트 실행으로 대체 금지).
6. 키는 환경변수로만. 채팅·파일·로그·커밋에 넣지 않는다. credential 문자열이 있는 diff는 외부 LLM에 보내지 않는다. 키 값은 진단 출력에도 찍지 않는다(길이와 문제 종류만).
7. 모델 ID를 추측하지 않는다. 비용은 단가가 설정된 경우에만 계산, 아니면 unknown. mock 벤치마크 결과엔 항상 "MOCK — harness check only" 배너.
8. 가짜 이력 금지: 과거 프로젝트를 "DevOrch로 만들었다"고 꾸미는 로그·커밋·문서를 만들지 않는다.
9. **측정 전에 정의한다.** 벤치마크의 판정 정규식·오탐 기준·케이스는 실행 *전에* 확정한다. 결과를 보고 고치지 않는다 — 고치면 해당 케이스 notes 에 이력과 이유를 남기고
   이전 결과와 직접 비교하지 않는다. 관찰한 한계는 수정하지 말고 기록한다.
10. **통계의 단위는 독립 표본이다.** 같은 케이스의 반복 실행은 독립이 아니다. 결함 단위로 집계하고, 차이가 없으면 "차이를 확인하지 못했다"고 쓴다("같다"가 아니다).
11. **안전 장치는 일부러 깨 봐서 테스트가 잡는지 확인한다**(변형 확인). 비용·보안 거부는 LLM 호출 *전에* 일어나야 한다. 위험 수준은 올릴 수만 있고 낮출 수 없다.

## 지금 할 일 (우선순위)
1. **비용 범위의 hold-out 검증**: 4건으로 정한 출력 범위를 *새로운* 커밋 6~8개(`devorch review --git-range <커밋>~1..<커밋> --task "코드 변경 리뷰" --level high`)에 적용해 실제 값이 범위 안에 드는 비율을 센다.
   자격증명 형태 문자열(테스트용 가짜 키)이 든 커밋은 우리 검사가 거부하니 제외. 밖이면 범위를 넓히고(테스트 `LARGE` 에 관측값 추가) 좁히지 않는다. 더 큰 diff(1만 2천 토큰 초과)도 최소 1건.
2. **Claude Code 연결의 남은 부분**: (헤드리스 검증은 끝남) 사용자가 IDE 에서 `..\devorch-playground` 를 열어 서버 승인 UI 와 `.mcp.json` 프로젝트 범위 흐름을 확인하고, 사람 승인(`devorch approve`) 뒤 에이전트가
   `devorch_apply_changes` 로 커밋하는 뒷부분을 한 번 해 본다. 이후 mock 을 빼고 실제 리뷰어로 한 번(`--max-total-usd` 와 `limits.max_cost_usd` 사용).
3. **구현 단계 비용 실측 늘리기(n=1 → 여러 건)**: `devorch run`(구현 포함)을 소액으로 중간 크기 작업 여러 건 실행(작업 전에 `devorch analyze` 의 예상 범위를 적어 두고 실제와 비교 — hold-out).
   출력이 구현/리뷰 단계별 호출·토큰·비용을 따로 보여준다(`WorkflowRun.role_usage`). 범위(`IMPLEMENTATION_OUTPUT_TOKENS`)가 틀리면 넓히고 좁히지 않는다. MCP `analyze_task` 도 `implementation_counts` 를 넘기게 할 것.
4. **현실적 평가 세트**: 시드 결함 대신 실제 오픈소스 버그 수정 커밋(알려진 버그)으로 탐지율·비용 평가. 판정은 사람 확인이 섞인 정답 라벨 필요.
5. 위 데이터로 HIGH 기본 정책(리뷰어 3명)을 재검토: 단일 교차 회사 리뷰어 + 초점 프롬프트가 같은 탐지를 3분의 1 비용에 하는지. CRITICAL 의 독립 리뷰어 2명은 비용이 아니라 통제 규칙이라 측정만으로 바꾸지 않는다.
6. 포트폴리오 케이스 스터디의 "미검증" 항목을 실측으로 갱신. 한계를 숨기지 않는 것이 강점.

## 아직 만들지 말 것
Cost Optimizer / Adaptive Routing / 캐스케이드 — 4번(현실적 평가) 이후. Grok 어댑터 — 벤치마크로 이득이 확인된 뒤. FastAPI·DB·Next.js — 핵심 가치와 무관, 요구가 생길 때만.

## 작업 방식
변경 전 관련 테스트를 읽고, 변경 후 전체 `pytest`. 의미 있는 단언만 쓴다. 실패 경로(변조·우회·실패 처리)를 먼저 테스트한다(테스트 먼저 → 실패 확인 → 구현).
확인하지 못한 것은 "미검증", 추정은 "추정"이라고 쓴다. 기존 구조(`src/devorch/{core,providers,security,bench,dashboard,cli}`)를 따른다.
Windows 콘솔은 cp949 라 `≈` 같은 문자를 출력하면 UnicodeEncodeError 가 난다 — CLI 출력에는 ASCII/한글만 쓰고, 한글이 든 결과는 파일로 쓴 뒤 읽는다. 키 입력 안내는 클립보드 방식
(`$env:X = (Get-Clipboard -Raw).Trim()`)을 쓴다. 비용이 드는 실행은 항상 `--max-total-usd` 를 붙이고 사용자의 확인을 받는다.
