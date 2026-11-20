# 注册一个“登录时启动”的计划任务，用 pythonw.exe 在后台运行 notify_agent.py。
# 用法（普通 PowerShell，无需管理员）：
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1
# 卸载：
#   Unregister-ScheduledTask -TaskName msg-notifier -Confirm:$false

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "notify_agent.py"

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $py = (Get-Command python.exe).Source
    $pythonw = Join-Path (Split-Path $py) "pythonw.exe"
}
if (-not (Test-Path $pythonw)) { throw "找不到 pythonw.exe，请确认已安装 Python" }

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $here
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "msg-notifier" -Action $action -Trigger $trigger `
    -Settings $settings -Description "转发 Windows 通知到 Bark" -Force | Out-Null
Start-ScheduledTask -TaskName "msg-notifier"
Write-Host "已注册并启动计划任务 msg-notifier（$pythonw $script）"
