param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [int]$Port = 8000,
    [switch]$StartApi
)

$ErrorActionPreference = "Stop"
$process = $null
$stdout = Join-Path $PSScriptRoot "dashboard-uvicorn.stdout.log"
$stderr = Join-Path $PSScriptRoot "dashboard-uvicorn.stderr.log"

function Assert-PropertyExists($Object, [string]$Name) {
    if ($null -eq $Object -or $Object.PSObject.Properties.Name -notcontains $Name) {
        throw "Required property '$Name' does not exist."
    }
}

function Assert-NotNull($Value, [string]$Name) {
    if ($null -eq $Value) { throw "Required property '$Name' is null." }
}

function Assert-Equal($Actual, $Expected, [string]$Name) {
    if ($Actual -ne $Expected) { throw "$Name expected '$Expected', received '$Actual'." }
}

function Assert-InRange($Value, [double]$Minimum, [double]$Maximum, [string]$Name) {
    Assert-NotNull $Value $Name
    if ([double]$Value -lt $Minimum -or [double]$Value -gt $Maximum) {
        throw "$Name must be between $Minimum and $Maximum, received '$Value'."
    }
}

try {
    if ($StartApi) {
        $process = Start-Process -FilePath ".\.venv\Scripts\python.exe" `
            -ArgumentList "-m", "uvicorn", "api.app:app", "--host", "127.0.0.1", "--port", "$Port" `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
        $deadline = (Get-Date).AddSeconds(10)
        $ready = $false
        do {
            try { Invoke-WebRequest "$BaseUrl/docs" -UseBasicParsing -TimeoutSec 1 | Out-Null; $ready = $true } catch { Start-Sleep -Milliseconds 250 }
        } while (-not $ready -and (Get-Date) -lt $deadline -and -not $process.HasExited)
        if (-not $ready) { throw "Dashboard API did not become ready within 10 seconds." }
    }

    $summary = Invoke-RestMethod "$BaseUrl/api/dashboard/summary"
    foreach ($name in @("date_from", "date_to", "timezone", "branch_scope", "workflow_counts", "scores", "durations", "testing", "approvals")) {
        Assert-PropertyExists $summary $name
        Assert-NotNull $summary.$name "summary.$name"
    }

    $counts = $summary.workflow_counts
    foreach ($name in @("total", "completed", "failed", "running", "waiting", "pending", "cancelled", "success_rate_percent", "failure_rate_percent")) { Assert-PropertyExists $counts $name }
    $stateTotal = [int]$counts.completed + [int]$counts.failed + [int]$counts.running + [int]$counts.waiting + [int]$counts.pending + [int]$counts.cancelled
    Assert-Equal $stateTotal ([int]$counts.total) "workflow state total"
    $terminal = [int]$counts.completed + [int]$counts.failed
    if ($terminal -gt 0) {
        Assert-InRange $counts.success_rate_percent 0 100 "success_rate_percent"
        Assert-InRange $counts.failure_rate_percent 0 100 "failure_rate_percent"
        Assert-InRange ([double]$counts.success_rate_percent + [double]$counts.failure_rate_percent) 99.99 100.01 "terminal rates sum"
    }

    $scores = $summary.scores
    foreach ($name in @("evaluated_workflows", "unevaluated_workflows", "average_score", "excellent", "good", "acceptable", "poor", "critical", "provisional", "scoring_versions")) { Assert-PropertyExists $scores $name }
    $gradeTotal = [int]$scores.excellent + [int]$scores.good + [int]$scores.acceptable + [int]$scores.poor + [int]$scores.critical
    Assert-Equal $gradeTotal ([int]$scores.evaluated_workflows) "final grade distribution"
    if ([int]$scores.evaluated_workflows -gt 0) { Assert-InRange $scores.average_score 0 100 "average_score" }

    $durations = $summary.durations
    foreach ($name in @("workflows_with_duration", "discarded_workflows", "average_wall_clock_seconds", "p50_wall_clock_seconds", "p90_wall_clock_seconds", "p95_wall_clock_seconds", "average_active_seconds", "average_approval_wait_seconds")) { Assert-PropertyExists $durations $name }
    if ($null -ne $durations.p50_wall_clock_seconds) {
        Assert-InRange $durations.p90_wall_clock_seconds ([double]$durations.p50_wall_clock_seconds) ([double]::MaxValue) "P90"
        Assert-InRange $durations.p95_wall_clock_seconds ([double]$durations.p90_wall_clock_seconds) ([double]::MaxValue) "P95"
    }

    $testing = $summary.testing
    foreach ($name in @("executed", "passed", "failed", "not_executed", "pass_rate_percent", "workflows_with_warnings", "total_warnings", "average_warnings", "repair_required", "repair_successful", "repair_failed", "repair_success_rate_percent", "average_repair_attempts")) { Assert-PropertyExists $testing $name }
    Assert-Equal ([int]$testing.passed + [int]$testing.failed) ([int]$testing.executed) "testing executed"
    Assert-Equal ([int]$testing.executed + [int]$testing.not_executed) ([int]$counts.total) "testing total"

    $approvals = $summary.approvals
    foreach ($name in @("total_approvals_requested", "total_approvals_granted", "total_approvals_rejected", "workflows_with_pending_approval", "average_approval_wait_seconds", "longest_approval_wait_seconds", "most_requested_operations")) { Assert-PropertyExists $approvals $name }
    if ([int]$approvals.total_approvals_granted + [int]$approvals.total_approvals_rejected -gt [int]$approvals.total_approvals_requested) { throw "Resolved approvals exceed requested approvals." }

    $metrics = @("workflow_count", "completed_count", "failed_count", "success_rate", "average_score", "average_duration", "average_active_duration", "average_approval_wait", "tests_passed", "tests_failed", "repair_count", "warning_count")
    foreach ($metric in $metrics) {
        $series = Invoke-RestMethod "$BaseUrl/api/dashboard/timeseries?metric=$metric&interval=day"
        Assert-PropertyExists $series "metric"; Assert-Equal $series.metric $metric "timeseries metric"
        Assert-PropertyExists $series "points"; Assert-NotNull $series.points "timeseries.points"
        foreach ($point in $series.points) {
            foreach ($name in @("bucket_start", "bucket_end", "value", "count", "numerator", "denominator")) {
                Assert-PropertyExists $point $name
            }
            if ([string]::IsNullOrWhiteSpace($point.bucket_start)) { throw "bucket_start cannot be empty" }
            if ([string]::IsNullOrWhiteSpace($point.bucket_end)) { throw "bucket_end cannot be empty" }
            $start = [datetimeoffset]::Parse($point.bucket_start)
            $end = [datetimeoffset]::Parse($point.bucket_end)
            if ($end -le $start) { throw "bucket_end must be greater than bucket_start" }
        }
    }

    $agents = Invoke-RestMethod "$BaseUrl/api/dashboard/agents?limit=5"
    Assert-PropertyExists $agents "items"; Assert-NotNull $agents.items "agents.items"
    foreach ($agent in $agents.items) { foreach ($name in @("agent", "total_tasks", "completed_tasks", "failed_tasks", "waiting_tasks", "skipped_tasks", "completion_rate_percent", "average_attempts", "average_duration_seconds", "related_workflows", "related_files", "findings_count")) { Assert-PropertyExists $agent $name } }

    $attention = Invoke-RestMethod "$BaseUrl/api/dashboard/attention?limit=5"
    Assert-PropertyExists $attention "items"; Assert-NotNull $attention.items "attention.items"
    foreach ($item in $attention.items) { foreach ($name in @("severity", "reasons", "finding_codes", "pending_operation", "age_seconds")) { Assert-PropertyExists $item $name }; if ($item.reasons -is [string]) { throw "attention.reasons must be a list." } }

    $activity = Invoke-RestMethod "$BaseUrl/api/dashboard/activity?limit=20"
    Assert-PropertyExists $activity "items"; Assert-NotNull $activity.items "activity.items"
    foreach ($item in $activity.items) {
        foreach ($name in @("event_id", "thread_id", "branch_id", "project_name", "type", "status", "message", "timestamp", "related_event_id")) { Assert-PropertyExists $item $name }
        if ($item.type -in @("tool_started", "tool_completed", "tool_failed")) { throw "Technical tool event leaked into summarized activity." }
        if ($item.message -match "Ã") { throw "Activity message contains invalid UTF-8 mojibake." }
    }

    Write-Host "Dashboard validation passed"
    exit 0
} catch {
    Write-Error $_
    exit 1
} finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $null = $process.WaitForExit(5000)
    }
}
