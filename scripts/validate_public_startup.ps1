param([Parameter(Mandatory = $true)][string]$CloneRoot)

$ErrorActionPreference = 'Stop'
$process = $null
$listener = $null
$code = 1
$saved = @{}
$settings = @{
    OPENAI_API_KEY = 'dummy'
    OPENAI_MODEL = 'dummy-no-provider-call'
    OPENAI_BASE_URL = 'http://127.0.0.1:1/v1'
    LANGSMITH_TRACING = 'false'
    LANGCHAIN_TRACING_V2 = 'false'
    OTEL_EXPORTER_OTLP_ENDPOINT = ''
    OTEL_EXPORTER_OTLP_HEADERS = ''
    NOTIFICATION_WORKER_ENABLED = 'false'
    ALERT_EVALUATION_ENABLED = 'false'
    MCP_FACTORY_DEBUG = 'false'
}
try {
    $root = (Resolve-Path -LiteralPath $CloneRoot).Path
    $python = Join-Path $root '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'validation_environment_missing' }
    $output = Join-Path $root 'artifacts/public-startup'
    New-Item -ItemType Directory -Path $output -Force | Out-Null
    $settings.WORKSPACE_ROOT = Join-Path $output 'workspace'
    $settings.DATA_ROOT = Join-Path $output 'data'
    $settings.LANGGRAPH_CHECKPOINT_DB = Join-Path $output 'checkpoints.sqlite'
    $settings.WORKFLOW_EVENT_STORE_PATH = Join-Path $output 'events.sqlite'
    foreach ($key in $settings.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process')
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    $process = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'api.app:app', '--host', '127.0.0.1', '--port', "$port") -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $output 'stdout.log') -RedirectStandardError (Join-Path $output 'stderr.log')
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline -and -not $process.HasExited) {
        try {
            $response = Invoke-WebRequest "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 1
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 150
        $process.Refresh()
    }
    if (-not $ready) { throw 'startup_health_failed' }
    $status = & curl.exe --silent --output NUL --write-out '%{http_code}' --max-time 2 "http://127.0.0.1:$port/docs"
    if ($LASTEXITCODE -ne 0 -or $status -ne '200') { throw 'startup_docs_failed' }
    Write-Output 'Health=200; Docs=200; provider calls disabled; private data not used.'
    $code = 0
} catch {
    Write-Output 'Public startup validation failed; inspect local redacted diagnostics before sharing.'
} finally {
    if ($listener) { $listener.Stop() }
    if ($process) {
        $process.Refresh()
        if (-not $process.HasExited) {
            try {
                $all = @(Get-CimInstance Win32_Process)
                $owned = @($process.Id)
                do {
                    $children = @($all | Where-Object { $_.ParentProcessId -in $owned -and $_.ProcessId -notin $owned } | ForEach-Object { $_.ProcessId })
                    $owned += $children
                } while ($children.Count -gt 0)
                $cleanup = Start-Process -FilePath 'taskkill.exe' -ArgumentList @('/PID', "$($process.Id)", '/T', '/F') -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput (Join-Path $output 'cleanup.stdout.log') -RedirectStandardError (Join-Path $output 'cleanup.stderr.log')
                $process.WaitForExit(5000) | Out-Null
                $process.Refresh()
                $remaining = @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -in $owned })
                if (-not $process.HasExited -or $remaining.Count -gt 0) {
                    Write-Output "Cleanup failed for validation PID $($process.Id); do not leave this process running."
                    $code = 1
                } else {
                    Write-Output 'Cleanup verified: validation process tree stopped.'
                }
            } catch {
                Write-Output "Cleanup failed for validation PID $($process.Id)."
                $code = 1
            }
        }
    }
    foreach ($key in $saved.Keys) {
        [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process')
    }
}
exit $code
