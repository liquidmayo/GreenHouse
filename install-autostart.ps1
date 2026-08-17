<#
  install-autostart.ps1 — register GreenHouse Monitor to start automatically at logon.

  Creates a Scheduled Task that runs start.bat (master) or start-agent.bat
  (companion agent) hidden via wscript, so nothing pops up. Re-running
  replaces the existing task. Uninstall with -Remove.

    powershell -ExecutionPolicy Bypass -File install-autostart.ps1            # master
    powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Agent     # companion agent
    powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Remove
#>
param(
  [switch] $Agent,
  [switch] $Remove
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$taskName = if ($Agent) { 'GreenHouse Monitor Agent' } else { 'GreenHouse Monitor Dashboard' }
$bat = if ($Agent) { 'start-agent.bat' } else { 'start.bat' }

if ($Remove) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "Removed scheduled task '$taskName'."
  exit 0
}

# hidden launcher: wscript runs the batch with window style 0
$vbs = Join-Path $root 'run-hidden.vbs'
@"
' Runs a batch file with no visible window (used by the auto-start task).
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "$root"
shell.Run "cmd.exe /c """ & "$root\$bat" & """", 0, False
"@ | Set-Content -Path $vbs -Encoding ASCII

$action  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT30S'   # let networking / other services settle first
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered '$taskName': runs $bat hidden 30s after $env:USERNAME logs on."
Write-Host "Test now:  Start-ScheduledTask -TaskName '$taskName'"
