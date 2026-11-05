# Bootstrap script: creates 2 isolated venvs for vibe-rebuild.
# pdf-sidecar and Open WebUI run via Docker (services/docker-compose.yml).
#
# Layout:
#   venv\          orchestrator (FastAPI + Playwright + ...)
#   venv-web\      web-sidecar  (httpx + readability-lxml + markdownify)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location (Split-Path $PSScriptRoot -Parent)
$root = (Get-Location).Path

function Write-Step([string] $msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Resolve-PythonExe {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "python.exe not found on PATH. Install Python 3.11+ first."
}

function New-OrUpdateVenv([string] $venvDir, [string] $requirementsPath) {
    $py = Resolve-PythonExe
    $venvPy = Join-Path $venvDir "Scripts\python.exe"

    if (-not (Test-Path $venvPy)) {
        Write-Step "Creating venv: $venvDir"
        & $py -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed: $venvDir" }
    } else {
        Write-Step "Updating venv: $venvDir"
    }

    & $venvPy -m pip install --upgrade pip wheel setuptools
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed in $venvDir" }

    if ($requirementsPath -and (Test-Path $requirementsPath)) {
        & $venvPy -m pip install -r $requirementsPath
        if ($LASTEXITCODE -ne 0) { throw "requirements install failed: $requirementsPath" }
    }
}

try {
    Write-Step "Resolving Python"
    $py = Resolve-PythonExe
    & $py --version

    # 1) orchestrator venv
    New-OrUpdateVenv `
        (Join-Path $root "venv") `
        (Join-Path $root "orchestrator\requirements.txt")

    # Playwright browser binaries (Chromium only is enough for screenshot review).
    $orchPy = Join-Path $root "venv\Scripts\python.exe"
    Write-Step "Installing Playwright Chromium"
    & $orchPy -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Playwright install failed (visual review will fall back to text review)" -ForegroundColor Yellow
    }

    # 2) web-sidecar venv
    New-OrUpdateVenv `
        (Join-Path $root "venv-web") `
        (Join-Path $root "services\web-sidecar\requirements.txt")

    Write-Host ""
    Write-Host "venvs ready:" -ForegroundColor Green
    Write-Host "  $root\venv          (orchestrator)"
    Write-Host "  $root\venv-web      (web-sidecar)"
    Write-Host ""
    Write-Host "Next: install Docker Desktop + run 'docker compose -f services\docker-compose.yml up -d'" -ForegroundColor DarkGray
    Write-Host "      (this brings up pdf-sidecar:8102 and open-webui:3100)" -ForegroundColor DarkGray
} catch {
    Write-Host ""
    Write-Host "Bootstrap failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
