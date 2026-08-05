# 매일 오전 6시 자동 실행을 Windows 작업 스케줄러에 등록한다.
#
# 실행:   powershell -ExecutionPolicy Bypass -File .\setup_schedule.ps1
# 시간 변경:   .\setup_schedule.ps1 -Time "07:30"
# 해제:   .\setup_schedule.ps1 -Remove

param(
    [string]$Time = "06:00",
    [switch]$Remove
)

$TaskName = 'NewsAssistant-DailyBriefing'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RunScript = Join-Path $ProjectDir 'run_daily.ps1'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "예약 작업 '$TaskName' 을(를) 삭제했습니다."
    exit 0
}

if (-not (Test-Path $RunScript)) {
    Write-Host "run_daily.ps1 을 찾을 수 없습니다: $RunScript" -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`"" `
    -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# 노트북을 쓰는 경우를 감안한 설정:
#   - 배터리 상태여도 실행 (기본값은 배터리면 건너뛴다)
#   - 6시에 PC가 꺼져 있었으면 켜진 뒤 최대한 빨리 실행
#   - 네트워크가 늦게 붙는 경우를 위해 재시도
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "매일 아침 뉴스 브리핑을 생성하고 Discord로 전송합니다." | Out-Null

Write-Host "등록 완료: '$TaskName' — 매일 $Time"
Write-Host ""
Write-Host "  지금 바로 테스트:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  상태 확인:         Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "  실행 로그:         $ProjectDir\logs\"
Write-Host "  해제:              .\setup_schedule.ps1 -Remove"
