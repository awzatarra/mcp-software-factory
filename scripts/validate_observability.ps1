param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$ApiUrl = "http://127.0.0.1:8000",
    [switch]$SkipApi
)

$ErrorActionPreference = "Stop"
$exitCode = 0

function Write-Check([string]$Name, [bool]$Passed) {
    Write-Host "$Name=$($Passed.ToString().ToLowerInvariant())"
    if (-not $Passed) { throw "$Name validation failed" }
}

function Invoke-WorkflowValidation([string]$BaseUrl) {
    $projectName = "observability-validation-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
    $request = "Crea un proyecto FastAPI llamado $projectName con GET /health que devuelva {`"status`": `"ok`"}, agrega pruebas y valida que pasen."
    $created = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/workflows" -ContentType "application/json" -Body (@{ request = $request } | ConvertTo-Json)
    $threadId = $created.thread_id
    $deadline = [DateTime]::UtcNow.AddMinutes(6)
    $snapshot = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        $snapshot = Invoke-RestMethod -Uri "$BaseUrl/api/workflows/$threadId" -TimeoutSec 10
        if ($snapshot.interrupted -and $snapshot.pending_operation) {
            Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/workflows/$threadId/approve" -ContentType "application/json" -Body (@{ reason = "Automated observability validation" } | ConvertTo-Json) | Out-Null
        } elseif (-not $snapshot.interrupted -and $snapshot.terminal_status -notin @("pending", "running", "waiting", "")) {
            break
        }
        Start-Sleep -Milliseconds 750
    }
    Write-Check "WorkflowCompleted" ($snapshot.terminal_status -eq "completed")
    $workflowTelemetry = Invoke-RestMethod -Uri "$BaseUrl/api/observability/workflows/$threadId" -TimeoutSec 10
    $trace = @($workflowTelemetry.traces | Where-Object { $_.branch_id -eq "original" })[-1]
    $detail = Invoke-RestMethod -Uri "$BaseUrl/api/observability/traces/$($trace.trace_id)" -TimeoutSec 10
    $spans = @($detail.spans)
    Write-Check "TraceExists" ($null -ne $trace.trace_id)
    Write-Check "RootSpanExists" (@($spans | Where-Object { $_.category -eq "workflow" }).Count -gt 0)
    Write-Check "PlanningSpanExists" (@($spans | Where-Object { $_.name -match "planning" }).Count -gt 0)
    Write-Check "ImplementationSpanExists" (@($spans | Where-Object { $_.name -match "implementation" }).Count -gt 0)
    Write-Check "TestingSpanExists" (@($spans | Where-Object { $_.category -eq "test" }).Count -gt 0)
    Write-Check "AgentSpansExist" (@($spans | Where-Object { $_.category -eq "agent" }).Count -gt 0)
    Write-Check "McpSpansExist" (@($spans | Where-Object { $_.category -eq "mcp" }).Count -gt 0)
    Write-Check "ApprovalSpanExists" (@($spans | Where-Object { $_.category -eq "approval" }).Count -gt 0)
    Write-Check "TestSpanExists" (@($spans | Where-Object { $_.category -eq "test" }).Count -gt 0)
    Write-Check "ArtifactsExist" (@($detail.artifacts).Count -gt 0)
    Write-Check "LogsCorrelated" (@($detail.logs | Where-Object { $_.trace_id -eq $trace.trace_id }).Count -gt 0)
    $serialized = $detail | ConvertTo-Json -Depth 12 -Compress
    Write-Check "NoSecretsFound" ($serialized -notmatch "Bearer\s+[^\[]|sk-[A-Za-z0-9_-]{8,}|OPENAI_API_KEY|developer prompt|system prompt")
    $secondRead = Invoke-RestMethod -Uri "$BaseUrl/api/observability/traces/$($trace.trace_id)" -TimeoutSec 10
    Write-Check "PersistedAfterRestart" ($secondRead.trace_id -eq $trace.trace_id)
}

try {
    & $Python -m pytest -q -k "observability or trace or span or telemetry"
    if ($LASTEXITCODE -ne 0) { throw "Observability tests failed" }
    & $Python scripts/backfill_observability.py --dry-run
    if ($LASTEXITCODE -ne 0) { throw "Backfill dry-run failed" }
    if (-not $SkipApi) {
        $summary = Invoke-RestMethod -Uri "$ApiUrl/api/observability/summary" -TimeoutSec 10
        if ($null -eq $summary.traces) { throw "Observability summary contract is invalid" }
        Write-Host "API summary validated: traces=$($summary.traces)"
        Invoke-WorkflowValidation $ApiUrl
    }
} catch {
    Write-Error $_
    $exitCode = 1
} finally {
    Write-Host "Observability validation finished with exit code $exitCode"
}
exit $exitCode
