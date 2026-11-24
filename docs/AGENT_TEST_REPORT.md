# DevOrch 테스트 기록: AI 에이전트(Claude)가 돌릴 수 있는 것을 전부 돌려 본 결과

> 이 기록은 **AI 에이전트(Anthropic Claude)가 직접 테스트하고 작성**했습니다. 사람 사용자의 피드백이 아닙니다.
> 기준: `main` 최신(2026-11-24 shallow clone). 표본이 작아 일반화하지 않습니다.

## 요약

- **단위 테스트 725개 전부 통과**, 9개 건너뜀(Docker 8 + Windows 배치 1). 코드 결함으로 보이는 실패 없음.
- **CLI 전체**(quickstart·policy·approve·finish·tasks·audit·usage·doctor·providers·sandbox·analyze·review·run·bench·dashboard)를 가짜 터미널(pty)로 실행해 확인.
- **MCP 사용 시나리오 15가지 이상**을 에이전트 입장에서 실행. 핵심 약속("리뷰·테스트·사람 승인 없이는 반영 안 됨")은 시험한 범위에서 전부 지켜짐.
- **발견한 개선점 8가지**(아래). 안전성을 깨는 것은 없었고, 대부분 UX·안내 문제와 탐지 범위에 대한 것.

## 테스트 환경과 한계 (중요)

이 세션은 `pip install`이 막혀 있어 README의 `pip install -e ".[dev,llm]"`를 그대로 할 수 없었습니다. 그래서 설치 없이 돌리려고 아래 장치를 **저장소 밖에** 만들었고, 저장소 코드는 전혀 수정하지 않았습니다.

1. **`typer` 호환 층** — 이 환경엔 `click`은 있고 `typer`가 없습니다. 실제 CLI 코드(`cli/main.py` 등)를 그대로 실행하기 위해 typer API를 click으로 감싸는 얇은 층을 만들었습니다. **진짜 typer가 아니므로** help 서식·완성 등 세부 동작은 다를 수 있고, 그 차이는 DevOrch 버그가 아닙니다.
2. **비동기 테스트 실행 플러그인** — `pytest-asyncio`가 없어, async 테스트를 실행만 해 주는 pytest 훅을 임시로 넣었습니다(`asyncio_mode=auto` 대체).
3. **LLM SDK 스텁** — `anthropic`/`openai`/`google-genai`가 없습니다. import만 되면 통과하는 테스트 11개를 위해, 네트워크 호출이 전혀 없는 가짜 클래스 3개를 import 경로에만 올렸습니다.
4. **MCP 런처/클라이언트** — `devorch mcp ... --mock`과 같은 일을 하는 런처와, stdio로 붙는 MCP 클라이언트를 만들어 에이전트 역할을 했습니다.
5. **사람 역할** — 승인·정책승인·finish의 대화형 확인은 `script`로 가짜 터미널(pty)을 만들어 실제 CLI에 입력을 넣는 방식으로 했습니다. **그래도 사람의 실제 사용 경험을 검증한 것은 아닙니다.**

**이 환경에서 못 한 것:** 실제 Claude Code/Cursor 앱 연결, 실제 LLM 리뷰어(mock만 씀), Docker 샌드박스(데몬 없음), Windows, API 키가 필요한 실호출.

## 1. 단위 테스트

`PYTHONPATH=src:<shim>:<sdk-stub> python -m pytest -q -p no:cacheprovider -p asyncshim`

| 단계 | 통과 | 실패/오류 | 건너뜀 |
|---|---|---|---|
| 설치 없이 그대로 | 172 | 46 실패 + 22 수집오류 | 8 |
| + async 실행 플러그인 | 213 | 11 실패 | 8 |
| + typer 호환 층 | 714 | 11 실패 | 9 |
| + LLM SDK 스텁(import만) | **725** | **0** | 9 |

- 모든 실패·오류는 **누락 패키지(`typer`, `pytest-asyncio`, LLM SDK)** 때문이었고, 그 자리를 메우자 전부 통과했습니다. 코드 결함은 보이지 않았습니다.
- 건너뜀 9개 = Docker 샌드박스 8 + Windows 배치 런처 1. 각자 환경이 없어 skip 처리되는 게 정상입니다.

## 2. CLI (가짜 터미널로 실제 실행)

| 명령 | 결과 |
|---|---|
| `quickstart --mock` | ✅ git 확인 → init → 점검 → 정책 승인 프롬프트(`y`) → MCP 등록 명령·첫 요청 예시까지 한 흐름으로 출력 |
| `policy status` / `accept` | ✅ diff를 보여 주고, 위험한 변경이 있으면 `y`가 아니라 `accept`를 직접 입력해야 승인됨. `y`만 치면 거부 |
| `approve` | ✅ diff 해시 앞 6자를 입력해야 승인. `y`·빈 Enter·틀린 해시는 전부 "기록 안 함". `n`은 거부 기록. 비대화형 실행은 거부(exit 2). `--diff`로 전체 diff 표시 |
| `finish` | ✅ 되돌릴 파일 목록 → `n` 취소/`y` 실행. 커밋된 내용과 다른 파일이 있으면 전체 차단 |
| `tasks` / `audit` / `usage` | ✅ 게이트 상태표, 서명 사슬 검증(32건 이상 없음), 작업·provider별 지출 집계 |
| `doctor` / `providers` / `sandbox` | ✅ 점검 결과와 해결 힌트, provider 가용성, 샌드박스 상태 |
| `analyze` / `review --mock` / `run --mock` | ✅ 위험도·영향·라우팅 출력, git 범위 리뷰, 전체 워크플로 시연(적용 없이 제안 저장) |
| `bench cases` / `bench run --mock` | ✅ 16개 시드 케이스 나열, 벤치 실행 후 `report.md`·`results.json` 생성(한계 고지 포함) |
| `dashboard` | ✅ 아래 별도 |

### 대시보드 (읽기 전용 로컬 서버)
- `/`, `/api/tasks`, `/api/audit`, `/api/bench`, `/api/tasks/<id>` 모두 200. 작업·게이트·감사 로그가 그대로 보임.
- 경로 우회 시도(`/api/tasks/../../etc/passwd`, URL 인코딩한 `..`)는 404/형식 오류로 거부.
- `POST /api/tasks` → 405, loopback이 아닌 `Host:` 헤더 → **403**(DNS 리바인딩 방어). 읽기 전용이라는 설명과 일치.

## 3. MCP 시나리오 (에이전트 입장)

| 시나리오 | 결과 |
|---|---|
| 정책 미승인 상태 호출 | ✅ 상태 변경 도구 전부 거부, 사람이 칠 명령 제공. `inspect_project`만 동작 |
| LOW: 할인 버그 수정 | ✅ 분석→수정→리뷰→테스트→커밋→finish→merge 완주 |
| HIGH/CRITICAL: auth 변경 | ✅ `approval_required`에서 멈춤 → 사람 승인(pty) → `committed` |
| `policy.yaml` 몰래 수정(유효값) | ✅ 모든 도구 멈춤("승인 뒤 바뀌었습니다") |
| **세션 중** 수정→원복 | ✅ 수정 중엔 멈추고, 원복하면 재승인 없이 다시 동작 |
| `policy.yaml` 잘못된 값 | ⚠️ 서버가 시작 못 함(아래 3번) |
| 가짜 승인 파일 직접 작성 | ✅ 서명 불일치로 무시, `note`로 설명 |
| 승인 뒤 코드 추가 수정 | ✅ 커밋엔 리뷰된 diff만. ⚠️ 에이전트에 경고 없음(아래 1번) |
| stale 승인(diff 바뀐 뒤 재사용) | ✅ "이전 diff 승인이라 무효" |
| 사람이 거부(`n`) | ✅ `apply`가 "거부됨"으로 차단, 작업 상태 `rejected` |
| 테스트 실패 구현 | ✅ `run_tests` failed → `apply` blocked, 확인용 worktree 보존 |
| NOT_READY→수정→재리뷰 | ✅ f-string SQL을 리뷰어가 HIGH로 차단 → 파라미터 바인딩으로 고치니 READY |
| 리뷰 라운드 한도(4회) | ✅ 5회째 "한도 도달, 사람이 검토". ⚠️ 4회째 안내 문구(아래 6번) |
| `protected_paths` | ✅ LOW를 HIGH로 올리고 리뷰어·승인 부착 |
| `test_weakening: enforce` | ✅ 테스트 삭제+skip을 HIGH로 올려 승인 필요 |
| 비밀값이 섞인 diff | ✅ `.env`는 diff에서 제외. ⚠️ 코드 안 토큰은 일부만 탐지(아래 7번) |
| MCP 리소스 | ✅ `devorch://policy`, `devorch://project`, `devorch://task/{id}` 동작, 잘못된 id 거부 |
| 샌드박스 없는 환경의 서명키 공격 | 재현 안 함 — README·`HOST_HARDENING.md`에 이미 "재현됨"으로 문서화된 한계 |

## 4. 발견한 것과 제안

### 1. 승인 뒤 바뀐 파일이 커밋에서 빠지는데 에이전트가 모름 (UX)
- 승인 → 작업 트리에 한 줄 추가 → `apply_changes` 하면 `status: committed`와 일반 안내만 옵니다. 추가분은 커밋에 없고, `finish`에서야 "커밋된 내용과 다른 파일이 있어…"로 드러납니다.
- 안전성은 맞습니다(`test_commit_rebuilds_from_the_reviewed_diff...` 의도대로). 문제는 에이전트가 "전부 커밋됐다"고 잘못 전할 수 있다는 것.
- **제안:** `apply_changes`에서 현재 작업 트리 diff ≠ `record.diff`이면 `committed` 응답에 "리뷰 이후 바뀐 파일(…)은 커밋에 포함되지 않았습니다" 경고 추가.

### 2. 거부·차단된 시도가 감사 로그에 안 남음
- approval_required, stale 승인, 가짜 승인 파일 무시, 테스트 없는 apply 차단, 사람 거부 — 이들 **차단 응답이 `audit.jsonl`에 기록되지 않습니다**. 성공 흐름과 `blocked_worktree_changed`, approval(사람이 직접 승인/거부한 것)만 남습니다.
- **제안:** 최소한 가짜 승인 파일 감지와 정책 변경으로 인한 거부는 서명된 로그에 남기기. 통제 도구에선 "누가 우회를 시도했나"가 가장 쓸모 있는 기록입니다.

### 3. `policy.yaml` 값이 잘못되면 에이전트가 원인을 못 봄
- `require_human_approval_at: none`(허용되지 않는 값)으로 두면 서버가 시작 단계에서 `ConfigError`로 종료합니다. MCP 클라이언트엔 `Connection closed`만 보입니다(실 CLI는 stderr에 한 줄 찍지만 에이전트가 사용자에게 전하기 어려움).
- 안전한 쪽으로 실패하는 건 좋습니다. **제안:** 설정 오류여도 서버는 띄우고 모든 도구가 그 오류 문구+`policy status`를 돌려주게 하기(정책 미승인 때와 같은 방식).

### 4. `how_to_approve`가 "그대로 붙여넣는 명령"이 아님
- 서버 지시문은 `how_to_approve`를 "완전한 명령이니 그대로 주라"고 하는데, 실제 값은 `사용자가 직접 터미널에서 다음을 실행해야 합니다(...): "python" -m devorch approve ...`처럼 **한국어 문장 + 명령**이 섞여 있습니다. `how_to_finish`·`merge_command`는 명령만 들어 있어 대비됩니다.
- **제안:** `approve_command` 필드를 따로 두고 설명은 `reason`에. Windows의 `[PowerShell 기준 …]` 꼬리말도 별도 필드로.

### 5. 위험도·영향 범위가 예측 밖 파일 변경을 표시 안 함
- "orders.py 문구 수정" 작업의 diff가 **`pricing.py` 함수 시그니처까지** 바꿨는데 위험도는 LOW 그대로였고, 예측 영향 밖이라는 표시도 없었습니다(review `notes`엔 "3개 파일 변경"만).
- **제안:** analyze가 예측한 direct/indirect 파일 밖을 diff가 건드리면 `notes`/`warnings`에 "예측 범위 밖 변경: …" 표시.

### 6. 위험도 키워드가 한국어 조사에 약함 (분류 누락)
- `analyze_task`의 위험도는 작업 문장 키워드도 봅니다. "**DB에서** 주문 조회"는 영역이 안 잡히고(빈 areas), "**DB 에서**"(띄어쓰기)나 "SQL"은 잡힙니다. 규칙 `\bdb\b`의 단어 경계가 "DB에서"처럼 조사가 붙은 한국어에서 안 맞기 때문입니다.
- README가 경로 기반 한계와 `protected_paths` 보완을 이미 설명하니 치명적이진 않지만, **한국어 UI 도구인 만큼** `db`·`sql` 등 영문 토큰의 한글 조사 결합을 규칙에 반영하면 좋겠습니다.
- 부차: 리뷰 라운드 4회째(마지막) NOT_READY에서도 `next_step`이 "다시 호출하세요"라고 안내합니다. 다음 호출은 한도 초과로 막히므로 마지막 라운드에선 "사람 검토" 쪽으로 안내하는 게 맞습니다.

### 7. 코드 안 비밀값 탐지 범위
- `get_diff`는 `.env`·키 파일을 diff에서 빼고, 코드에 들어간 **OpenAI/Anthropic `sk-`, AWS `AKIA`, GitHub `ghp_`, Google `AIza`, PEM 블록**은 탐지합니다.
- 다만 **Slack `xoxb-`, Stripe `sk_live_`, GitHub 세분화 토큰 `github_pat_`, JWT, `password=`/DB URL 속 비밀번호**는 현재 패턴에 안 걸립니다(`secret_redactor._PATTERNS` 직접 확인).
- **제안:** 흔한 토큰 몇 가지(특히 Slack·Stripe·`github_pat_`)를 패턴에 추가. "완전하지 않다"는 전제라면 그 한계를 문서에 한 줄 더.

### 8. 작은 것들
- `doctor`의 "provider 없음" 힌트는 `devorch run ... --mock`을 권하는데, README 첫 안내는 `devorch quickstart --mock`입니다. 처음 쓰는 사람에겐 하나로 통일하면 덜 헷갈립니다.
- `init` 직후 `.devorch/config.yaml`·`policy.yaml`이 untracked로 남아 `inspect_project`가 `dirty: true`를 보고합니다. 두 파일을 커밋하라는 건지 두라는 건지 한 줄 안내가 있으면 좋겠습니다(흐름 자체는 안 막힘).

## 결론

통제 엔진으로서의 핵심 불변식 — **리뷰·테스트·사람 승인 게이트, 정책 변조 감지, 서명 승인, 승인 후 diff 고정, 감사 사슬** — 은 시험한 범위에서 모두 의도대로 작동했습니다. 남은 것은 대부분 "안전하게 막긴 했는데 에이전트/사용자가 이유를 알기 어렵다"는 종류의 UX·안내와, 탐지 범위(한국어 키워드, 비밀값 패턴) 문제입니다.

---

## 추가: 진짜 설치로 재확인 (2026-11-24, 같은 날 다른 세션)

> 이 절도 AI 에이전트(Claude)가 직접 실행하고 작성했습니다. 위 기록의 "테스트 환경과 한계"에서 빠졌던 부분을 메우는 것이 목적입니다.

### 환경
- `main` 최신 `cd887a0`(2026-11-21), Linux · Python 3.13.18
- `pip install -e ".[dev,llm]"` **정상 설치** — 위의 임시 장치(typer 호환 층, async 플러그인, LLM SDK 가짜 클래스) **없음**
- 설치된 주요 패키지: typer 0.29.2, pytest-asyncio 1.5.0, mcp 2.3.0, pydantic 2.14.4, anthropic 2.13.0, openai 3.58.0, google-genai 3.6.1, pytest 9.2.0

### 단위 테스트 비교 (수정 전 `main` 그대로)

| | 위 기록(임시 장치) | 진짜 설치 |
|---|---|---|
| `python -m pytest` | 725 통과 · 9 건너뜀 · 0 실패 | **725 통과 · 9 건너뜀 · 0 실패** (2회 같음) |
| 건너뜀 구성 | Docker 8 + Windows 배치 1 | 같음 |
| Docker 샌드박스 테스트 8개 | 못 함(데몬 없음) | **8개 통과**(일반 사용자) — 아래 한계 참고 |
| 맨 `pytest` | 확인 안 함 | **수집 단계에서 멈춤, 0개 실행** (새로 발견 1) |

→ 위 기록의 "모든 실패는 누락 패키지 때문, 코드 결함 없음" 결론은 진짜 설치에서도 맞았습니다. LLM SDK 가짜 클래스로 통과시켰던 11개도 진짜 SDK로 통과합니다.

### CLI 재확인 (진짜 typer, 대화형이 아닌 명령만)
`init`, `providers`, `bench cases`, `tasks`, `audit`, `usage`, `analyze` 정상. `doctor` 는 API 키가 없어 "사용 가능한 provider 없음"으로 종료 코드 1(정상 동작).
**다시 돌리지 않은 것:** `approve`·`finish`·`policy accept`·`quickstart`(대화형), 대시보드, MCP 시나리오 — 이 부분은 여전히 위의 호환 층 결과만 있습니다.

### 새로 발견한 것
1. **맨 `pytest` 수집 오류** — `tests/test_dashboard.py` 만 `from tests.test_mcp import ...` 를 써서, 루트가 import 경로에 없는 `pytest` 실행에서 멈춤. CI·위 기록은 `python -m pytest` 라 드러나지 않았음.
2. **root 로 실행하면 Docker 샌드박스도 root** — 컨테이너가 호스트 uid 로 도는데, DevOrch 가 root 면 컨테이너 안도 root. root 로 돌리자 격리 점검 10개 중 2개 실패(일반 사용자 아님, `/work`·`/tmp` 밖 쓰기 가능). 설명("never root")과 코드가 달랐음.

### 위 기록 지적 중 다시 확인한 것
- **§6 한국어 조사:** 재현됨. "DB에서 주문 조회" LOW(5점) / "DB 에서 주문 조회" HIGH(35점).

### 고친 것 (커밋 3개, 아직 저장소에 올리지 않음)
| 커밋 | 내용 | 테스트 |
|---|---|---|
| 1 | 새로 발견 1: import 를 다른 테스트와 같은 방식(`from test_mcp import`)으로 | 맨 `pytest` 0개 → 725 통과 |
| 2 | 새로 발견 2: 호스트 root 면 기본 거부(호스트 실행으로 대체 안 함), `project.sandbox.user: "uid:gid"`(0 금지)로 지정, `doctor`·`sandbox` 가 미리 표시 | 새 19개 + 실제 Docker 1개, 변형 5종 |
| 3 | §6: `re:` 키워드를 ASCII 경계로(한글 이웃 = 경계). §7: Slack·Stripe 운영 키·`github_pat_` 는 막고, JWT·URL 속 비밀번호는 가리기만 | 새 28개, 변형 8종 |

수정 후 전체: **일반 사용자 + Docker 781 통과 · 1 건너뜀 · 0 실패**, root 773 통과 · 9 건너뜀 · 0 실패.

### 이 절의 한계
- Docker 테스트는 이 환경에서 Docker Hub·ECR·GCR 미러·ghcr.io 가 모두 막혀, GitHub 의 Python 3.12.13 이식형 빌드로 만든 대체 베이스 이미지에 저장소의 `tests/docker/Dockerfile` 을 **그대로** 빌드해 돌렸습니다. 검증한 것은 DevOrch 쪽 격리 설정이고, 진짜 `python:3.12-slim` 확인은 CI 몫입니다.
- Windows·macOS, rootless Docker, 실제 Claude Code 연결, 실제 LLM 리뷰어는 이번에도 확인하지 않았습니다.
- 위 기록 §1~5·§8 과 §6 의 "마지막 리뷰 라운드 안내 문구"는 손대지 않았습니다.
