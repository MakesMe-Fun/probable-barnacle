# 매일 아침 자동 실행용 스크립트 (Windows 작업 스케줄러가 호출)
#
# 등록:   .\setup_schedule.ps1
# 수동 실행 테스트:   powershell -ExecutionPolicy Bypass -File .\run_daily.ps1
#
# 실행 결과는 logs\ 아래에 날짜별로 남는다. 아침에 알림이 안 왔으면 그 파일을 보면 된다.

$ErrorActionPreference = 'Continue'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ProjectDir

$LogDir = Join-Path $ProjectDir 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("run_{0}.log" -f (Get-Date -Format 'yyyyMMdd'))

# 파이썬 로그가 cp949로 깨지지 않게
$env:PYTHONIOENCODING = 'utf-8'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 실행 시작 ===" | Out-File -FilePath $LogFile -Append -Encoding utf8

# 예약 실행이라 브라우저는 띄우지 않는다. 알림은 Discord로 간다.
python main.py --no-browser 2>&1 | Out-File -FilePath $LogFile -Append -Encoding utf8
$exit = $LASTEXITCODE

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 종료 (exit=$exit) ===" | Out-File -FilePath $LogFile -Append -Encoding utf8

# 30일 지난 로그는 정리
Get-ChildItem $LogDir -Filter 'run_*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $exit
