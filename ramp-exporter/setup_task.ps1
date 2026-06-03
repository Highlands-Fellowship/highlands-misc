# Register daily Windows Task Scheduler jobs for Ramp -> Sage export.
# Run once from an elevated (Administrator) PowerShell prompt:
#   .\setup_task.ps1
#
# Adjust SCRIPT_DIR, PYTHON_EXE, and run hours below to match your machine.

$SCRIPT_DIR      = "C:\ramp-exporter"
$PYTHON_EXE      = "python"          # or full path e.g. C:\Python312\python.exe
$CARD_HOUR       = 6                 # card transactions: 6 AM daily
$REIMB_HOUR      = 6                 # reimbursements:    6 AM daily (can differ)
$BILL_HOUR       = 6                 # bill payments:     6 AM daily (can differ)
$CARD_PMT_HOUR   = 7                 # card payments:     7 AM daily (after card export)

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

# --- Bill payments ---
$billAction = New-ScheduledTaskAction `
    -Execute $PYTHON_EXE `
    -Argument "$SCRIPT_DIR\billpay.py --mark-synced" `
    -WorkingDirectory $SCRIPT_DIR

$billTrigger = New-ScheduledTaskTrigger -Daily -At "${BILL_HOUR}:00"

Register-ScheduledTask `
    -TaskName   "RampBillPayExport" `
    -Action     $billAction `
    -Trigger    $billTrigger `
    -Settings   $settings `
    -RunLevel   Highest `
    -Description "Daily Ramp bill payment export to Sage 50" `
    -Force

# --- Card payments (clears open AP invoices from card transaction export) ---
$cardPmtAction = New-ScheduledTaskAction `
    -Execute $PYTHON_EXE `
    -Argument "$SCRIPT_DIR\card_payment.py" `
    -WorkingDirectory $SCRIPT_DIR

$cardPmtTrigger = New-ScheduledTaskTrigger -Daily -At "${CARD_PMT_HOUR}:00"

Register-ScheduledTask `
    -TaskName   "RampCardPaymentExport" `
    -Action     $cardPmtAction `
    -Trigger    $cardPmtTrigger `
    -Settings   $settings `
    -RunLevel   Highest `
    -Description "Daily Ramp card statement payment export to Sage 50" `
    -Force

Write-Host ""
Write-Host "Tasks registered:"
Write-Host "  RampCardExport          -- runs daily at ${CARD_HOUR}:00 AM"
Write-Host "  RampReimbursementExport -- runs daily at ${REIMB_HOUR}:00 AM"
Write-Host "  RampBillPayExport       -- runs daily at ${BILL_HOUR}:00 AM"
Write-Host "  RampCardPaymentExport   -- runs daily at ${CARD_PMT_HOUR}:00 AM"
Write-Host ""
Write-Host "To test immediately:"
Write-Host "  Start-ScheduledTask -TaskName 'RampCardExport'"
Write-Host "  Start-ScheduledTask -TaskName 'RampReimbursementExport'"
Write-Host "  Start-ScheduledTask -TaskName 'RampBillPayExport'"
Write-Host "  Start-ScheduledTask -TaskName 'RampCardPaymentExport'"
