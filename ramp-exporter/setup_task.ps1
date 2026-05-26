# Register a daily Windows Task Scheduler job for Ramp → Sage export.
# Run once from an elevated (Administrator) PowerShell prompt:
#   .\setup_task.ps1
#
# Adjust SCRIPT_DIR and PYTHON_EXE below to match your machine.

$SCRIPT_DIR = "C:\ramp-to-sage"
$PYTHON_EXE = "python"          # or full path e.g. C:\Python312\python.exe
$TASK_NAME  = "RampToSageExport"
$RUN_HOUR   = 6                 # 6 AM daily

$action  = New-ScheduledTaskAction `
    -Execute $PYTHON_EXE `
    -Argument "$SCRIPT_DIR\main.py" `
    -WorkingDirectory $SCRIPT_DIR

$trigger = New-ScheduledTaskTrigger -Daily -At "${RUN_HOUR}:00"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName   $TASK_NAME `
    -Action     $action `
    -Trigger    $trigger `
    -Settings   $settings `
    -RunLevel   Highest `
    -Description "Daily Ramp card transaction export to Sage 50" `
    -Force

Write-Host ""
Write-Host "Task '$TASK_NAME' registered."
Write-Host "It will run daily at $RUN_HOUR:00 AM."
Write-Host "To test immediately: Start-ScheduledTask -TaskName '$TASK_NAME'"
