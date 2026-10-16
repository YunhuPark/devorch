# 시연용 플레이그라운드를 처음 상태로 되돌린다 (add() 가 뺄셈을 하는 버그 상태, DevOrch 작업 기록 없음).
#   확인만:   powershell -File tools\demo\reset_playground.ps1 -Path C:\...\devorch-playground
#   실제 실행: ... 뒤에 -Yes
# 지우는 것: devorch/* 브랜치, .devorch 의 tasks/runs/worktrees/audit* (작업·감사 기록), 작업 트리의 미커밋 수정.
# 건드리지 않는 것: config.yaml, policy.yaml, .mcp.json, CLAUDE.md, 코드 파일(저장소가 기준 커밋과 같을 때).
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$Base = "4123eae",   # 버그가 있고 설정까지 끝난 기준 커밋
    [switch]$Yes
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path $Path).Path
foreach ($must in @("CLAUDE.md", "calc.py", ".mcp.json", ".devorch\config.yaml")) {
    if (-not (Test-Path (Join-Path $root $must))) { throw "플레이그라운드가 아닌 것 같아 중단합니다 ($must 없음): $root" }
}
function G { & git -C $root @args; if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') 실패" } }
G rev-parse --verify -q "$Base^{commit}" | Out-Null

$branch = (& git -C $root branch --show-current)
$branches = @(& git -C $root for-each-ref --format="%(refname:short)" "refs/heads/devorch/")
$head = (& git -C $root rev-parse --short HEAD)
$dirty = @(& git -C $root status --porcelain)
Write-Host "대상: $root"
Write-Host "현재 브랜치: $branch  HEAD: $head  (기준 커밋 $Base)"
Write-Host "지울 devorch 브랜치: $(if ($branches) { $branches -join ', ' } else { '없음' })"
Write-Host "되돌릴 미커밋 변경: $(if ($dirty) { ($dirty -join ' | ') } else { '없음' })"
Write-Host "지울 기록: .devorch\tasks, runs, worktrees, audit.jsonl, audit.head, audit.jsonl.lock"
if ($head -ne (& git -C $root rev-parse --short $Base)) { Write-Host "※ main 이 기준 커밋과 다릅니다. 기준 커밋으로 되돌립니다(되돌려지는 커밋은 reflog 에 남습니다)." }
if (-not $Yes) { Write-Host "`n확인만 했습니다. 실제로 하려면 -Yes 를 붙이세요."; exit 0 }

if ($branch -ne "main") { G checkout -q main }
G reset -q --hard $Base
G clean -fdq -e .devorch
foreach ($b in $branches) { G branch -q -D $b }
& git -C $root worktree prune
foreach ($d in @("tasks", "runs", "worktrees")) { Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root ".devorch\$d") }
foreach ($f in @("audit.jsonl", "audit.head", "audit.jsonl.lock")) { Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $root ".devorch\$f") }
Write-Host "`n완료. 상태:"
G status --short
Write-Host "calc.py: $((Get-Content (Join-Path $root 'calc.py') | Select-Object -Skip 1 -First 1).Trim())"
