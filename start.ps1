# vibe-rebuild orchestration:
#   - venv\         orchestrator    (port 8000, /v1/* + /api/*)
#   - venv-web\     web-sidecar     (port 8004, /web/fetch)
#   - docker        pdf-sidecar     (port 8002, /pdf/from-{markdown,html})
#   - docker        open-webui      (port 3000, chat UI)
#   - native        cloudflared     (vibeitz.com → 3000)
#
# All 5 processes (2 host + 2 docker container + 1 cloudflared) are bound to a
# Win32 Job Object so closing this terminal kills the host children. Docker
# containers persist; we explicitly `docker compose down` in the finally block.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
$root = $PSScriptRoot

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
$publicHost = if ($env:VIBE_PUBLIC_HOSTNAME) { $env:VIBE_PUBLIC_HOSTNAME } else { "vibeitz.com" }
$tunnelName = if ($env:VIBE_TUNNEL_NAME) { $env:VIBE_TUNNEL_NAME } else { "vibeitz" }
$cloudflaredConfig = if ($env:CLOUDFLARED_CONFIG) {
    $env:CLOUDFLARED_CONFIG
} else {
    Join-Path $env:USERPROFILE ".cloudflared\config.yml"
}

$useDocker = ($env:VIBE_USE_DOCKER -ne "false")  # default true
$useTunnel = ($env:VIBE_USE_TUNNEL -ne "false")  # default true

$logsDir = Join-Path $root "logs"
$orchOut = Join-Path $logsDir "start-orchestrator.out.log"
$orchErr = Join-Path $logsDir "start-orchestrator.err.log"
$webOut  = Join-Path $logsDir "start-web-sidecar.out.log"
$webErr  = Join-Path $logsDir "start-web-sidecar.err.log"
$tunnelOut = Join-Path $logsDir "start-tunnel.out.log"
$tunnelErr = Join-Path $logsDir "start-tunnel.err.log"

$orchProc = $null
$webProc  = $null
$tunnelProc = $null
$jobHandle = [IntPtr]::Zero
$dockerStarted = $false
$exitCode = 0

# ---------------------------------------------------------------------------
# Win32 Job Object — kills host children when this terminal closes
# ---------------------------------------------------------------------------
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class VibeJobObject {
    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr hJob, int JobObjectInfoClass, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr hObject);

    public static IntPtr CreateKillOnCloseJob() {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) {
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr infoPtr = Marshal.AllocHGlobal(length);
        try {
            Marshal.StructureToPtr(info, infoPtr, false);
            if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, infoPtr, (uint)length)) {
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
        } finally {
            Marshal.FreeHGlobal(infoPtr);
        }
        return job;
    }
}
"@

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step([string] $msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Note([string] $msg) { Write-Host "    $msg" -ForegroundColor DarkGray }

function Add-ToRuntimeJob([System.Diagnostics.Process] $process) {
    if ($null -eq $process -or $jobHandle -eq [IntPtr]::Zero) { return }
    $ok = [VibeJobObject]::AssignProcessToJobObject($jobHandle, $process.Handle)
    if (-not $ok) {
        $err = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        Write-Host "Process job 연결 실패(pid=$($process.Id), win32=$err). 종료 시 직접 정리합니다." -ForegroundColor Yellow
    }
}

function Stop-StartedProcess([System.Diagnostics.Process] $process) {
    if ($null -eq $process) { return }
    try {
        $process.Refresh()
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

function Wait-HttpOk([string] $url, [int] $seconds, [string] $name) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) {
                Write-Note "$name healthy ($($r.StatusCode))"
                return $r.StatusCode
            }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    return $null
}

function Resolve-Cloudflared {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $root "cloudflared.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"),
        "C:\Cloudflared\bin\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe",
        "C:\Program Files (x86)\cloudflared\cloudflared.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

function Stop-VibeRuntime-Stale {
    # 같은 워크트리 내에서 떠 있던 잔존 process 정리.
    $rootPattern = [regex]::Escape($root)
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -ieq "python.exe" -and $_.CommandLine -match $rootPattern -and
                ($_.CommandLine -match "server\.py" -or $_.CommandLine -match "web-sidecar")) -or
            ($_.Name -ieq "cloudflared.exe" -and $_.CommandLine -match "tunnel" -and
                $_.CommandLine -match "run" -and $_.CommandLine -match [regex]::Escape($tunnelName))
        } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Wait-VibeRuntime {
    while ($true) {
        Start-Sleep -Seconds 2
        $orchProc.Refresh()
        $webProc.Refresh()
        if ($orchProc.HasExited) { throw "orchestrator (8000) exited. Check $orchErr" }
        if ($webProc.HasExited)  { throw "web-sidecar (8004) exited. Check $webErr" }
        if ($useTunnel -and $null -ne $tunnelProc) {
            $tunnelProc.Refresh()
            if ($tunnelProc.HasExited) { throw "Cloudflare Tunnel exited. Check $tunnelErr" }
        }
    }
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
try {
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $root "data\open-webui") -Force | Out-Null
    Remove-Item $orchOut, $orchErr, $webOut, $webErr, $tunnelOut, $tunnelErr -ErrorAction SilentlyContinue
    $jobHandle = [VibeJobObject]::CreateKillOnCloseJob()

    # venv check
    $orchPy = Join-Path $root "venv\Scripts\python.exe"
    $webPy  = Join-Path $root "venv-web\Scripts\python.exe"
    if (-not (Test-Path $orchPy)) { throw "orchestrator venv missing: $orchPy. Run scripts\bootstrap_venvs.ps1 first." }
    if (-not (Test-Path $webPy))  { throw "web-sidecar venv missing: $webPy. Run scripts\bootstrap_venvs.ps1 first." }

    # token
    $tokenFile = Join-Path $root ".vibe_token"
    if (-not $env:PORT_PROJECT_INTERNAL_TOKEN) {
        if (Test-Path $tokenFile) {
            $env:PORT_PROJECT_INTERNAL_TOKEN = (Get-Content $tokenFile -Raw).Trim().TrimStart([char]0xFEFF)
        } else {
            $env:PORT_PROJECT_INTERNAL_TOKEN = & $orchPy -c "import secrets; print(secrets.token_urlsafe(32))"
            [System.IO.File]::WriteAllText(
                $tokenFile,
                $env:PORT_PROJECT_INTERNAL_TOKEN,
                [System.Text.UTF8Encoding]::new($false)
            )
        }
    }
    $env:VIBE_INTERNAL_TOKEN = $env:PORT_PROJECT_INTERNAL_TOKEN

    if (-not $env:VIBE_PUBLIC_BASE_URL) {
        $env:VIBE_PUBLIC_BASE_URL = "https://$publicHost"
    }

    Write-Step "Stopping stale processes from a previous run"
    Stop-VibeRuntime-Stale
    Start-Sleep -Seconds 1

    # ---- Docker (pdf-sidecar + open-webui) ----
    if ($useDocker) {
        $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
        $composePath = Join-Path $root "services\docker-compose.yml"
        if (-not $dockerCmd) {
            Write-Host "Docker not on PATH — skipping pdf-sidecar / open-webui." -ForegroundColor Yellow
            Write-Note "Install Docker Desktop (winget install Docker.DockerDesktop) and rerun start.ps1."
        } elseif (-not (Test-Path $composePath)) {
            throw "docker-compose.yml not found: $composePath"
        } else {
            Write-Step "Starting pdf-sidecar + open-webui via docker compose"
            docker compose -f $composePath up -d 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw "docker compose up -d failed (exit $LASTEXITCODE)"
            }
            $dockerStarted = $true
        }
    }

    # ---- orchestrator (port 8100) ----
    Write-Step "Starting orchestrator on http://127.0.0.1:8100"
    $orchProc = Start-Process `
        -FilePath $orchPy `
        -ArgumentList "server.py" `
        -WorkingDirectory (Join-Path $root "orchestrator") `
        -WindowStyle Hidden `
        -RedirectStandardOutput $orchOut `
        -RedirectStandardError $orchErr `
        -PassThru
    Add-ToRuntimeJob $orchProc

    if (-not (Wait-HttpOk "http://127.0.0.1:8100/healthz" 30 "orchestrator")) {
        throw "orchestrator did not respond at /healthz. Check $orchErr"
    }

    # ---- web-sidecar (port 8104) ----
    Write-Step "Starting web-sidecar on http://127.0.0.1:8104"
    $webProc = Start-Process `
        -FilePath $webPy `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8104") `
        -WorkingDirectory (Join-Path $root "services\web-sidecar") `
        -WindowStyle Hidden `
        -RedirectStandardOutput $webOut `
        -RedirectStandardError $webErr `
        -PassThru
    Add-ToRuntimeJob $webProc

    if (-not (Wait-HttpOk "http://127.0.0.1:8104/healthz" 30 "web-sidecar")) {
        throw "web-sidecar did not respond at /healthz. Check $webErr"
    }

    # ---- docker health (only if compose came up) ----
    if ($dockerStarted) {
        if (-not (Wait-HttpOk "http://127.0.0.1:8102/health" 60 "pdf-sidecar")) {
            Write-Host "pdf-sidecar /health did not come up. Check 'docker compose logs pdf-sidecar'." -ForegroundColor Yellow
        }
        if (-not (Wait-HttpOk "http://127.0.0.1:3100/health" 90 "open-webui")) {
            Write-Host "open-webui /health did not come up. Check 'docker compose logs open-webui'." -ForegroundColor Yellow
        }
    }

    # ---- Cloudflare Tunnel (Open WebUI 3000 → vibeitz.com) ----
    if ($useTunnel) {
        $cloudflared = Resolve-Cloudflared
        if (-not $cloudflared) {
            Write-Host "cloudflared not found — skipping tunnel." -ForegroundColor Yellow
            Write-Note "Install: winget install --id Cloudflare.cloudflared"
        } elseif (-not (Test-Path $cloudflaredConfig) -and -not $env:CLOUDFLARED_TUNNEL_TOKEN) {
            Write-Host "cloudflared config not found ($cloudflaredConfig) and no CLOUDFLARED_TUNNEL_TOKEN. Skipping tunnel." -ForegroundColor Yellow
        } else {
            Write-Step "Starting Cloudflare Tunnel for https://$publicHost"
            if ($env:CLOUDFLARED_TUNNEL_TOKEN) {
                $tunArgs = @("tunnel", "--no-autoupdate", "--loglevel", "info", "run", "--token", $env:CLOUDFLARED_TUNNEL_TOKEN)
            } else {
                $tunArgs = @("tunnel", "--config", $cloudflaredConfig, "--loglevel", "info", "run", $tunnelName)
            }
            $tunnelProc = Start-Process `
                -FilePath $cloudflared `
                -ArgumentList $tunArgs `
                -WorkingDirectory $root `
                -WindowStyle Hidden `
                -RedirectStandardOutput $tunnelOut `
                -RedirectStandardError $tunnelErr `
                -PassThru
            Add-ToRuntimeJob $tunnelProc

            if (-not (Wait-HttpOk "https://$publicHost/" 60 "cloudflare tunnel")) {
                Write-Host "tunnel did not respond at https://$publicHost. Check $tunnelErr" -ForegroundColor Yellow
            }
        }
    }

    Write-Host ""
    Write-Host "vibe-rebuild is running." -ForegroundColor Green
    Write-Host "  orchestrator : http://127.0.0.1:8100  (/v1/* + /api/*)"
    Write-Host "  web-sidecar  : http://127.0.0.1:8104"
    if ($dockerStarted) {
        Write-Host "  pdf-sidecar  : http://127.0.0.1:8102  (docker)"
        Write-Host "  open-webui   : http://127.0.0.1:3100  (docker)"
    }
    if ($useTunnel -and $null -ne $tunnelProc) {
        Write-Host "  public       : https://$publicHost  (cloudflared)"
        try { Set-Clipboard -Value "https://$publicHost"; Write-Host "  (public URL copied to clipboard)" -ForegroundColor DarkGray } catch {}
    }
    Write-Host "  logs         : $logsDir"
    Write-Host ""
    Write-Host "Press Ctrl+C to stop everything." -ForegroundColor DarkGray
    Write-Host ""

    Wait-VibeRuntime
} catch {
    $exitCode = 1
    Write-Host ""
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    foreach ($pair in @(
        @{ name = "orchestrator"; log = $orchErr },
        @{ name = "web-sidecar";  log = $webErr  },
        @{ name = "tunnel";       log = $tunnelErr }
    )) {
        if (Test-Path $pair.log) {
            Write-Host "$($pair.name) log tail:" -ForegroundColor Yellow
            Get-Content $pair.log -Tail 15 -ErrorAction SilentlyContinue
            Write-Host ""
        }
    }
} finally {
    Write-Host ""
    Write-Step "Stopping vibe-rebuild"
    Stop-StartedProcess $tunnelProc
    Stop-StartedProcess $webProc
    Stop-StartedProcess $orchProc

    if ($dockerStarted) {
        $composePath = Join-Path $root "services\docker-compose.yml"
        Write-Step "Stopping docker services (pdf-sidecar + open-webui)"
        docker compose -f $composePath down 2>&1 | Out-Host
    }

    if ($jobHandle -ne [IntPtr]::Zero) {
        [VibeJobObject]::CloseHandle($jobHandle) | Out-Null
    }
}

exit $exitCode
