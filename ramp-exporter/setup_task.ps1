# Register daily Windows Task Scheduler jobs for Ramp -> Sage export.
# Run once from an elevated (Administrator) PowerShell prompt:
#   .\setup_task.ps1
#
# Adjust SCRIPT_DIR, PYTHON_EXE, and run hours below to match your machine.

$SCRIPT_DIR   = "C:\ramp-exporter"
$PYTHON_EXE   = "python"          # or full path e.g. C:\Python312\python.exe
$CARD_HOUR    = 6                 # card transactions: 6 AM daily
$REIMB_HOUR   = 6                 # reimbursements:    6 AM daily (can differ)

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# --- Card transactions ---
$cardAction = New-ScheduledTaskAction `
    -Execute $PYTHON_EXE `
    -Argument "$SCRIPT_DIR\main.py --mark-synced" `
    -WorkingDirectory $SCRIPT_DIR

$cardTrigger = New-ScheduledTaskTrigger -Daily -At "${CARD_HOUR}:00"

Register-ScheduledTask `
    -TaskName   "RampCardExport" `
    -Action     $cardAction `
    -Trigger    $cardTrigger `
    -Settings   $settings `
    -RunLevel   Highest `
    -Description "Daily Ramp card transaction export to Sage 50" `
    -Force

# --- Reimbursements ---
$reimbAction = New-ScheduledTaskAction `
    -Execute $PYTHON_EXE `
    -Argument "$SCRIPT_DIR\reimburse.py --mark-synced" `
    -WorkingDirectory $SCRIPT_DIR

$reimbTrigger = New-ScheduledTaskTrigger -Daily -At "${REIMB_HOUR}:00"

Register-ScheduledTask `
    -TaskName   "RampReimbursementExport" `
    -Action     $reimbAction `
    -Trigger    $reimbTrigger `
    -Settings   $settings `
    -RunLevel   Highest `
    -Description "Daily Ramp reimbursement export to Sage 50" `
    -Force

Write-Host ""
Write-Host "Tasks registered:"
Write-Host "  RampCardExport          -- runs daily at ${CARD_HOUR}:00 AM"
Write-Host "  RampReimbursementExport -- runs daily at ${REIMB_HOUR}:00 AM"
Write-Host ""
Write-Host "To test immediately:"
Write-Host "  Start-ScheduledTask -TaskName 'RampCardExport'"
Write-Host "  Start-ScheduledTask -TaskName 'RampReimbursementExport'"
