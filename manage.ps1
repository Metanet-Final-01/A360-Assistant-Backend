<#
.SYNOPSIS
  A360 백엔드 전체 환경(도커 db·opensearch·backend)을 한 번에 켜고 끈다.

.DESCRIPTION
  .env가 없으면 .env.example에서 만들고 JWT_SECRET을 자동 발급한다.
  docker compose로 전체 스택을 올리고 db가 준비될 때까지 기다린다.

.EXAMPLE
  .\manage.ps1 up            # 전체 기동 (필요 시 .env 생성)
  .\manage.ps1 up -Build     # 이미지 재빌드 후 기동
  .\manage.ps1 down          # 전체 종료
  .\manage.ps1 restart       # 재시작
  .\manage.ps1 status        # 상태 확인
  .\manage.ps1 logs          # 전체 로그 (Ctrl+C로 종료)
  .\manage.ps1 logs backend  # 특정 서비스 로그
  .\manage.ps1 reset         # 볼륨까지 삭제 (DB 데이터 초기화 — 주의)
  .\manage.ps1 smoke         # 손으로 확인용 서버 기동 — 관측 로그가 팀 공유 DB로 안 가게 격리
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet("up", "down", "restart", "status", "logs", "reset", "smoke")]
  [string]$Action = "up",

  [Parameter(Position = 1)]
  [string]$Service,

  [switch]$Build
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }

# docker compose(v2) / docker-compose(v1) 자동 선택
function Invoke-Compose {
  param([string[]]$ComposeArgs)
  $probe = docker compose version 2>$null
  if ($LASTEXITCODE -eq 0) {
    & docker compose @ComposeArgs
  } else {
    & docker-compose @ComposeArgs
  }
}

# .env 없으면 .env.example에서 생성하고 JWT_SECRET 자동 발급
function Initialize-EnvFile {
  if (Test-Path ".env") { return }
  if (-not (Test-Path ".env.example")) {
    throw ".env 도 .env.example 도 없습니다. 리포 루트에서 실행하세요."
  }
  Write-Step ".env 이 없어 .env.example 에서 생성합니다"
  Copy-Item ".env.example" ".env"

  # JWT_SECRET 자동 발급 (미설정 시 앱이 기동 거부하므로)
  $bytes = New-Object 'System.Byte[]' 48
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $secret = [Convert]::ToBase64String($bytes) -replace '[+/=]', ''
  (Get-Content ".env") -replace '^JWT_SECRET=.*$', "JWT_SECRET=$secret" |
    Set-Content ".env" -Encoding utf8
  Write-Ok "JWT_SECRET 자동 발급 완료"

  # OPENAI_API_KEY 는 실제 시크릿이라 자동 생성 불가 — 안내
  $envText = Get-Content ".env" -Raw
  if ($envText -match '(?m)^OPENAI_API_KEY=[ \t]*(#.*)?$') {
    Write-Warn2 ".env 의 OPENAI_API_KEY 가 비어 있습니다 — LLM/임베딩 기능을 쓰려면 채우세요."
  }
}

function Wait-DbHealthy {
  Write-Step "DB 준비 대기 중..."
  for ($i = 0; $i -lt 30; $i++) {
    $state = (docker inspect -f '{{.State.Health.Status}}' a360-postgres 2>$null)
    if ($state -eq "healthy") { Write-Ok "DB 준비 완료"; return }
    Start-Sleep -Seconds 2
  }
  Write-Warn2 "DB 헬스체크가 30초 내 healthy 가 되지 않았습니다 (로그 확인: .\manage.ps1 logs db)"
}

function Show-Urls {
  $port = "8000"
  if (Test-Path ".env") {
    $m = Select-String -Path ".env" -Pattern '^APP_PORT=(\d+)' | Select-Object -First 1
    if ($m) { $port = $m.Matches[0].Groups[1].Value }
  }
  Write-Host ""
  Write-Ok  "백엔드   : http://localhost:$port/docs  (Swagger)"
  Write-Host "     헬스   : http://localhost:$port/api/health"
  Write-Host "     디버그 : http://localhost:$port/debug/debug.html"
  Write-Host "     상태   : .\manage.ps1 status   |  로그: .\manage.ps1 logs"
}

switch ($Action) {
  "up" {
    Initialize-EnvFile
    $composeArgs = @("up", "-d")
    if ($Build) { $composeArgs += "--build" }
    Write-Step ("docker compose " + ($composeArgs -join " "))
    Invoke-Compose $composeArgs
    Wait-DbHealthy
    Invoke-Compose @("ps")
    Show-Urls
  }
  "down" {
    Write-Step "전체 종료 (docker compose down)"
    Invoke-Compose @("down")
    Write-Ok "종료 완료 (DB 데이터는 볼륨에 보존)"
  }
  "restart" {
    Invoke-Compose @("down")
    Initialize-EnvFile
    $composeArgs = @("up", "-d")
    if ($Build) { $composeArgs += "--build" }
    Invoke-Compose $composeArgs
    Wait-DbHealthy
    Show-Urls
  }
  "status" {
    Invoke-Compose @("ps")
  }
  "logs" {
    if ($Service) { Invoke-Compose @("logs", "-f", "--tail", "100", $Service) }
    else { Invoke-Compose @("logs", "-f", "--tail", "100") }
  }
  "smoke" {
    # 손으로 API를 찔러보는(live smoke) 용도로 서버를 띄우되, **관측 로그가 팀 공유 Neon에
    # 가지 않게** 격리한다 (RPA-168).
    #
    # 왜 필요한가: tests/conftest.py의 격리 픽스처는 **pytest 전용**이다. `.env` 그대로
    # uvicorn을 띄우면 관측 미들웨어가 공유 DB에 그대로 쓴다 — 2026-07-15에 실제로 발생해
    # request_metrics 25행·audit_logs 22행을 오염시켰다. CONVENTIONS §8이 live 증명을
    # 요구하므로 live smoke 자체는 계속 필요하다. 끄고 돌리면 된다.
    Initialize-EnvFile

    # ⚠️ `$env:X = ""` 는 쓰면 안 된다 — PowerShell이 변수를 **삭제**해버려서 load_dotenv()가
    #    .env에서 값을 다시 읽어온다(실측 2026-07-15). 공백 한 칸을 넣으면 환경에는 존재하되
    #    observability_url()의 .strip()에서 빈 값이 돼 로컬 앱 DB로 폴백한다.
    $env:OBSERVABILITY_DATABASE_URL = " "

    # 격리를 바라지 말고 확인한다 — 위 메커니즘이 언제든 깨질 수 있으므로, 조용히 오염시키는
    # 대신 시끄럽게 멈춘다(fail-closed).
    # ⚠️ 프로브는 반드시 `import app.db` 를 먼저 해야 한다 — load_dotenv()가 거기서만 돈다.
    #    observability_db 만 import하면 .env가 안 읽혀 **항상 LOCAL이 나오는 가짜 가드**가 된다
    #    (실측 2026-07-15: 그렇게 짰다가 잡음).
    Write-Step "관측 DB 격리 확인 중..."
    $target = python -c "import app.db; from app.core.observability_db import observability_url; print('LOCAL' if not observability_url() else 'SHARED')"
    if ($LASTEXITCODE -ne 0) { throw "격리 확인 실패 — 의존성이 설치됐는지 확인하세요." }
    if ($target -ne "LOCAL") {
      throw "관측 쓰기가 여전히 공유 DB로 향합니다 ($target). 팀 데이터 오염을 막기 위해 기동을 중단합니다."
    }
    Write-Ok "관측 쓰기 = 로컬 앱 DB (공유 Neon 미접촉)"

    $port = "8000"
    if (Test-Path ".env") {
      $m = Select-String -Path ".env" -Pattern '^APP_PORT=(\d+)' | Select-Object -First 1
      if ($m) { $port = $m.Matches[0].Groups[1].Value }
    }
    Write-Step "uvicorn 기동 (Ctrl+C 로 종료) — http://localhost:$port/docs"
    Write-Warn2 "이 창에서만 격리됩니다. 다른 창에서 그냥 uvicorn 을 띄우면 공유 DB에 씁니다."
    python -m uvicorn app.main:app --reload --port $port
  }
  "reset" {
    Write-Warn2 "볼륨(DB 데이터 포함)까지 모두 삭제합니다."
    $confirm = Read-Host "정말 진행할까요? (yes 입력)"
    if ($confirm -eq "yes") {
      Invoke-Compose @("down", "-v")
      Write-Ok "볼륨 삭제 완료 — 다음 up 은 빈 DB로 시작합니다"
    } else {
      Write-Host "취소됨"
    }
  }
}
