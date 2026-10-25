<#
.SYNOPSIS
Sets up brainsync (the SSVEP side) on a Windows laptop: a .venv-win venv with the repo,
PsychoPy and the Cerelog BrainFlow build, then checks each piece without hardware.

.DESCRIPTION
An addition for Windows: the macOS / Linux setup in the README (.venv, the vendored
BrainFlow, deploy/jetson) is unchanged and still the main one. Runs in Windows Python
(not WSL), because only Windows sees the board's COM port and only a native window gets
a steady 60 Hz flicker.

Needs Python 3.12 (py install 3.12; PsychoPy 2026.2 supports 3.10-3.12 and brainsync
needs 3.11+), and for the first BrainFlow build Visual Studio with "Desktop development
with C++" plus git (see deploy\windows\INSTALL.md).

    powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1
#>
param(
    [string]$Venv = "",
    [string]$Python = "",
    [string]$Wheel = "",
    [switch]$FullPsychoPy,
    [switch]$SkipBrainFlow
)
$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $Venv) { $Venv = Join-Path $RepoRoot ".venv-win" }
$BuildScript = Join-Path $PSScriptRoot "brainflow\build_brainflow.ps1"
$WheelDir = Join-Path $env:LOCALAPPDATA "BrainSync\brainflow\out"
$Check = Join-Path $RepoRoot "deploy\jetson\brainflow\check_brainflow.py"

function Log($msg) { Write-Host "`n==> $msg" }
function Die($msg) { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

if (-not $Python) {
    $Python = (& py -V:3.12 -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $Python) { Die "no Python 3.12; install it with: py install 3.12 (or pass -Python)" }
}
$py = Join-Path $Venv "Scripts\python.exe"

# 1. the venv and the repo
if (-not (Test-Path $py)) {
    Log "creating $Venv with $Python"
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Die "could not create $Venv" }
}
$pip = @("-m", "pip", "install", "--disable-pip-version-check", "--retries", "10", "--timeout", "60")
Log "installing brainsync (editable) into $Venv"
$extras = if ($FullPsychoPy) { "[dev,display]" } else { "[dev]" }
& $py @pip "setuptools<81" -e "$RepoRoot$extras"     # brainflow imports pkg_resources
if ($LASTEXITCODE -ne 0) { Die "pip install -e failed" }

# 2. PsychoPy: by default only what the stimulus imports (psychopy-lean.txt)
if (-not $FullPsychoPy) {
    Log "installing PsychoPy (lean: psychopy --no-deps + deploy\windows\psychopy-lean.txt)"
    & $py @pip --no-deps "psychopy==2026.2.4"
    if ($LASTEXITCODE -ne 0) { Die "could not install psychopy" }
    & $py @pip -r (Join-Path $PSScriptRoot "psychopy-lean.txt")
    if ($LASTEXITCODE -ne 0) { Die "could not install psychopy-lean.txt" }
    Write-Host "(pip's 'psychopy requires ..., which is not installed' lines are expected: the stimulus uses none of those)"
}

# 3. BrainFlow with Cerelog X8 support: a given wheel, else the last build, else build it
if (-not $SkipBrainFlow) {
    if (-not $Wheel) {
        $last = Get-ChildItem (Join-Path $WheelDir "brainflow-*-win_amd64.whl") -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($last) { $Wheel = $last.FullName }
    }
    if (-not $Wheel) {
        Log "no BrainFlow wheel yet; building it"
        # not captured, so the fetch and build progress (and any error) stay on screen
        & powershell -NoProfile -ExecutionPolicy Bypass -File $BuildScript -Python $Python | Out-Host
        if ($LASTEXITCODE -ne 0) { Die "BrainFlow build failed; see the output above" }
        $built = Get-ChildItem (Join-Path $WheelDir "brainflow-*-win_amd64.whl") -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $built) { Die "the build finished but left no wheel in $WheelDir" }
        $Wheel = $built.FullName
    }
    Log "installing $Wheel"
    & $py @pip --force-reinstall --no-deps $Wheel
    if ($LASTEXITCODE -ne 0) { Die "could not install $Wheel" }
}

# 4. checks without hardware
$failed = @()
if (-not $SkipBrainFlow) {
    Log "BrainFlow check (deploy\jetson\brainflow\check_brainflow.py)"
    & $py -I $Check
    if ($LASTEXITCODE -ne 0) { $failed += "BrainFlow" }
}
Log "PsychoPy window backend"
& $py -c "import psychopy.visual.backends.pygletbackend; from psychopy import visual, event, core; print('psychopy', __import__('psychopy').__version__, 'imports OK')"
if ($LASTEXITCODE -ne 0) { $failed += "PsychoPy" }
Log "EEG board"
& $py -c "from brainsync.board import detect_serial_port, serial_search_note; p = detect_serial_port(); print('board on ' + p if p else 'no board plugged in (looked for ' + serial_search_note() + ')')"
if ($LASTEXITCODE -ne 0) { $failed += "brainsync import" }

if ($failed) { Die "failed: $($failed -join ', ')" }
Log "ready. Next: deploy\windows\check.ps1, then $py -m brainsync.demo (deploy\windows\INSTALL.md)"
