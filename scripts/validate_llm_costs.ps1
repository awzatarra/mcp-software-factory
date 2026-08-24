param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$database = Join-Path ([System.IO.Path]::GetTempPath()) ("llm-cost-validation-" + [guid]::NewGuid().ToString("N") + ".sqlite")

try {
    Push-Location $root
    & $Python -m pytest -q tests/test_llm_costs.py
    if ($LASTEXITCODE -ne 0) { exit 1 }

    & $Python scripts/import_llm_pricing.py --database $database --file config/llm-pricing.example.json --dry-run
    if ($LASTEXITCODE -ne 0) { exit 1 }

    & $Python scripts/backfill_llm_costs.py --database $database
    if ($LASTEXITCODE -ne 0) { exit 1 }
    & $Python scripts/backfill_llm_costs.py --database $database
    if ($LASTEXITCODE -ne 0) { exit 1 }

    & $Python scripts/reconcile_llm_costs.py --database $database
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Output "LLM cost validation completed"
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
finally {
    Pop-Location -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $database -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath ($database + "-wal") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath ($database + "-shm") -Force -ErrorAction SilentlyContinue
}
