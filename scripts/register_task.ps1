# PowerShell script to register "Mobisa Mailer Worker" scheduled task in Windows Task Scheduler

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$PythonW = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$WorkerScript = Join-Path $ProjectRoot "worker.py"

if (-not (Test-Path $PythonW)) {
    Write-Error "Virtual environment pythonw.exe not found at $PythonW. Run setup first."
}

$TaskName = "Mobisa Mailer Worker"
$Action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$WorkerScript`" --run" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At 18:00
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 7) `
    -MultipleInstances IgnoreNew `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries
# Runs only while this user is logged on, so no password is stored.
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Write-Host "Registering scheduled task '$TaskName'..."
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Mobisa Mailer background worker for sending cold emails" `
    -Force

Write-Host "Task '$TaskName' registered successfully to run daily at 18:00 IST."
