param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [Parameter(Mandatory = $true)][string]$WorkflowId,
    [ValidateSet("full", "prepare-restart", "verify-restart")][string]$Mode = "full",
    [string]$RestartStatePath = "data/agent-evaluation-restart-validation.json"
)

$ErrorActionPreference = "Stop"
$base = $BaseUrl.TrimEnd("/")

function Invoke-JsonRequest {
    param([string]$Method, [string]$Path, [object]$Body = $null)
    $arguments = @{ Method = $Method; Uri = "$base$Path"; ContentType = "application/json" }
    if ($null -ne $Body) { $arguments.Body = ($Body | ConvertTo-Json -Depth 10 -Compress) }
    Invoke-RestMethod @arguments
}

if ($Mode -eq "verify-restart") {
    if (-not (Test-Path -LiteralPath $RestartStatePath)) { throw "Restart validation state not found" }
    $restartState = Get-Content -LiteralPath $RestartStatePath -Raw -Encoding utf8 | ConvertFrom-Json
    $persisted = Invoke-JsonRequest GET "/api/evaluations/runs/$($restartState.evaluation_run_id)"
    if ($persisted.workflow_id -ne $restartState.workflow_id) { throw "Evaluation did not survive API restart" }
    [ordered]@{
        workflow_id = $persisted.workflow_id
        evaluation_run_id = $persisted.evaluation_run_id
        restart = "passed"
        persistence = "passed"
    } | ConvertTo-Json
    exit 0
}

$request = @{ workflow_id = $WorkflowId; branch_id = "original"; evaluation_type = "deterministic" }
$first = Invoke-JsonRequest POST "/api/evaluations/runs" $request
$second = Invoke-JsonRequest POST "/api/evaluations/runs" $request
if ($first.evaluation_run_id -ne $second.evaluation_run_id) { throw "Idempotency validation failed" }

$request.force = $true
$forced = Invoke-JsonRequest POST "/api/evaluations/runs" $request
if ($forced.evaluation_run_id -eq $first.evaluation_run_id) { throw "Force evaluation did not create a new run" }
if ($forced.overall_score -lt 0 -or $forced.overall_score -gt 1) { throw "Score is outside 0..1" }

if ($Mode -eq "prepare-restart") {
    $stateDirectory = Split-Path -Parent $RestartStatePath
    if ($stateDirectory) { New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null }
    @{ workflow_id = $WorkflowId; evaluation_run_id = $forced.evaluation_run_id } |
        ConvertTo-Json | Set-Content -LiteralPath $RestartStatePath -Encoding utf8
    [ordered]@{
        workflow_id = $WorkflowId
        evaluation_run_id = $forced.evaluation_run_id
        restart = "pending_api_restart"
        next_mode = "verify-restart"
    } | ConvertTo-Json
    exit 0
}

$baseline = Invoke-JsonRequest POST "/api/evaluations/baselines" @{
    scope = @{ workflow_id = $WorkflowId; branch_id = "original" }
    metric = "workflow_score"
    score = [double]$forced.overall_score
    sample_count = 1
}
$comparison = Invoke-JsonRequest POST "/api/evaluations/compare" @{
    evaluation_run_id = $forced.evaluation_run_id
    baseline_id = $baseline.baseline_id
    regression_threshold = 0.05
}
$detail = Invoke-JsonRequest GET "/api/evaluations/runs/$($forced.evaluation_run_id)"
$serialized = $detail | ConvertTo-Json -Depth 20
if ($serialized -match "sk-[A-Za-z0-9]" -or $serialized -match "Authorization" -or $serialized -match "OPENAI_API_KEY") { throw "Sensitive data found in evaluation response" }

$runs = Invoke-JsonRequest GET "/api/evaluations/runs?workflow=$([uri]::EscapeDataString($WorkflowId))"
$agents = Invoke-JsonRequest GET "/api/evaluations/agents"
$metrics = Invoke-JsonRequest GET "/api/evaluations/metrics"
$rubrics = Invoke-JsonRequest GET "/api/evaluations/rubrics"
$dashboard = Invoke-JsonRequest GET "/api/evaluations/dashboard"
if ($runs.total -lt 2 -or $rubrics.items.Count -lt 1) { throw "Durable API validation failed" }

[ordered]@{
    workflow_id = $WorkflowId
    idempotent_run_id = $first.evaluation_run_id
    forced_run_id = $forced.evaluation_run_id
    score = $forced.overall_score
    verdict = $forced.verdict
    baseline_id = $baseline.baseline_id
    regressions = $comparison.regressions.Count
    agents = $agents.items.Count
    metrics = $metrics.items.Count
    workflows_evaluated = $dashboard.workflows_evaluated
    security = "passed"
    persistence = "passed_in_current_process"
    restart = "use prepare-restart then verify-restart"
} | ConvertTo-Json -Depth 10
