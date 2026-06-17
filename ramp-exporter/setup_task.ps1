# Register Windows Task Scheduler jobs for Ramp -> Sage export.
# Run from an elevated (Administrator) PowerShell prompt.
#
# Usage:
#   .\setup_task.ps1                                  # register all four tasks
#   .\setup_task.ps1 -Tasks Card                      # card transactions only
#   .\setup_task.ps1 -Tasks CardPayment               # card payments only
#   .\setup_task.ps1 -Tasks Reimb                     # reimbursements only
#   .\setup_task.ps1 -Tasks Bill                      # bill payments only
#   .\setup_task.ps1 -Tasks Card,Reimb                # multiple tasks
#
# Valid task names: Card, CardPayment, Reimb, Bill

param(
    [string[]]$Tasks = @("Card", "CardPayment", "Reimb", "Bill")
)

# ── Configuration ────────────────────────────────────────────────────────────
$SCRIPT_DIR      = "C:\ramp-exporter"
$PYTHON_EXE      = "python"          # or full path e.g. C:\Python312\python.exe
$CARD_HOUR       = 6                 # card transactions: 6 AM daily
$REIMB_HOUR      = 6                 # reimbursements:    6 AM daily (can differ)
$BILL_HOUR       = 6                 # bill payments:     6 AM daily (can differ)
$CARD_PMT_HOUR   = 8                 # card payments:     8 AM daily (2hr after card export;
                                     # script also holds CSV if card transactions haven't been
                                     # exported to exported_ids.json yet — so timing is secondary)
# ─────────────────────────────────────────────────────────────────────────────

$validTasks = @("Card", "CardPayment", "Reimb", "Bill")
foreach ($t in $Tasks) {
    if ($t -notin $validTasks) {
        Write-Error "Unknown task '$t'. Valid options: $($validTasks -join ', ')"
        exit 1
    }
}

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

$registered = @()

# ── Card transactions ─────────────────────────────────────────────────────────
if ("Card" -in $Tasks) {
    $action  = New-ScheduledTaskAction `
        -Execute $PYTHON_EXE `
        -Argument "$SCRIPT_DIR\main.py --mark-synced" `
        -WorkingDirectory $SCRIPT_DIR
    $trigger = New-ScheduledTaskTrigger -Daily -At "${CARD_HOUR}:00"
    Register-ScheduledTask `
        -TaskName    "RampCardExport" `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -RunLevel    Highest `
        -Description "Daily Ramp card transaction export to Sage 50" `
        -Force | Out-Null
    $registered += "  RampCardExport          -- daily at ${CARD_HOUR}:00 AM"
}

# ── Card payments ─────────────────────────────────────────────────────────────
if ("CardPayment" -in $Tasks) {
    $action  = New-ScheduledTaskAction `
        -Execute $PYTHON_EXE `
        -Argument "$SCRIPT_DIR\card_payment.py" `
        -WorkingDirectory $SCRIPT_DIR
    $trigger = New-ScheduledTaskTrigger -Daily -At "${CARD_PMT_HOUR}:00"
    Register-ScheduledTask `
        -TaskName    "RampCardPaymentExport" `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -RunLevel    Highest `
        -Description "Daily Ramp card statement payment export to Sage 50" `
        -Force | Out-Null
    $registered += "  RampCardPaymentExport   -- daily at ${CARD_PMT_HOUR}:00 AM"
}

# ── Reimbursements ────────────────────────────────────────────────────────────
if ("Reimb" -in $Tasks) {
    $action  = New-ScheduledTaskAction `
        -Execute $PYTHON_EXE `
        -Argument "$SCRIPT_DIR\reimburse.py --mark-synced" `
        -WorkingDirectory $SCRIPT_DIR
    $trigger = New-ScheduledTaskTrigger -Daily -At "${REIMB_HOUR}:00"
    Register-ScheduledTask `
        -TaskName    "RampReimbursementExport" `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -RunLevel    Highest `
        -Description "Daily Ramp reimbursement export to Sage 50" `
        -Force | Out-Null
    $registered += "  RampReimbursementExport -- daily at ${REIMB_HOUR}:00 AM"
}

# ── Bill payments ─────────────────────────────────────────────────────────────
if ("Bill" -in $Tasks) {
    $action  = New-ScheduledTaskAction `
        -Execute $PYTHON_EXE `
        -Argument "$SCRIPT_DIR\billpay.py --mark-synced" `
        -WorkingDirectory $SCRIPT_DIR
    $trigger = New-ScheduledTaskTrigger -Daily -At "${BILL_HOUR}:00"
    Register-ScheduledTask `
        -TaskName    "RampBillPayExport" `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -RunLevel    Highest `
        -Description "Daily Ramp bill payment export to Sage 50" `
        -Force | Out-Null
    $registered += "  RampBillPayExport       -- daily at ${BILL_HOUR}:00 AM"
}

Write-Host ""
Write-Host "Tasks registered:"
$registered | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host "To test immediately:"
if ("Card"        -in $Tasks) { Write-Host "  Start-ScheduledTask -TaskName 'RampCardExport'" }
if ("CardPayment" -in $Tasks) { Write-Host "  Start-ScheduledTask -TaskName 'RampCardPaymentExport'" }
if ("Reimb"       -in $Tasks) { Write-Host "  Start-ScheduledTask -TaskName 'RampReimbursementExport'" }
if ("Bill"        -in $Tasks) { Write-Host "  Start-ScheduledTask -TaskName 'RampBillPayExport'" }
