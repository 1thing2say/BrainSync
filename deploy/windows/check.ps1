<#
.SYNOPSIS
Windows pre-flight for the SSVEP demo: power, USB, the board's COM port and a measured
60 Hz refresh. Prints PASS / WARN / FAIL per line and a go / no-go line.

.DESCRIPTION
An addition for Windows laptops; macOS and Linux need none of it. On the first Windows
runs the board dropped off USB twice and frames took up to 20 ms instead of 16.7 ms,
both on battery. This catches those before the headset goes on. Opens a black
full-screen window for about 6 s to measure frame timing (-NoWindow skips it).

    powershell -ExecutionPolicy Bypass -File deploy\windows\check.ps1
#>
param(
    [string]$Venv = "",
    [double]$ExpectedHz = 60.0,
    [switch]$NoWindow
)
$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $Venv) { $Venv = Join-Path $RepoRoot ".venv-win" }
$py = Join-Path $Venv "Scripts\python.exe"
$results = @()

function Report($status, $name, $detail) {
    $color = @{ PASS = "Green"; WARN = "Yellow"; FAIL = "Red" }[$status]
    Write-Host ("{0,-4}  {1,-16} {2}" -f $status, $name, $detail) -ForegroundColor $color
    $script:results += $status
}

# power: on battery Windows may suspend USB ports and slow the GPU
Add-Type -AssemblyName System.Windows.Forms
$line = [System.Windows.Forms.SystemInformation]::PowerStatus.PowerLineStatus
if ($line -eq "Online") { Report PASS "charger" "plugged in" }
else { Report FAIL "charger" "on battery: plug in the charger (USB suspend, unsteady frames)" }

$scheme = (powercfg /getactivescheme) -join " "
if ($scheme -match "\(([^)]+)\)") { $plan = $Matches[1] } else { $plan = $scheme }
if ($plan -match "saver") { Report WARN "power plan" "$plan; switch to Balanced or High performance" }
else { Report PASS "power plan" $plan }

# USB selective suspend (sub group USB settings, setting USB selective suspend)
$q = (powercfg /query SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226) -join "`n"
if ($q -match "Current AC Power Setting Index:\s*0x0*([0-9a-f]+)") {
    if ($Matches[1] -eq "0") { Report PASS "USB suspend" "disabled on AC" }
    else {
        Report WARN "USB suspend" ("enabled on AC; disable it: powercfg /setacvalueindex SCHEME_CURRENT " +
            "2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0; powercfg /setactive SCHEME_CURRENT")
    }
} else { Report WARN "USB suspend" "setting not exposed on this machine; keep the charger in" }

# the board: a present USB serial adapter, and the port brainsync would open
$adapters = Get-PnpDevice -PresentOnly -Class Ports -ErrorAction SilentlyContinue |
    Where-Object { $_.InstanceId -match "VID_(1A86|303A|10C4|0403)" }
if (-not (Test-Path $py)) {
    Report FAIL "venv" "$py missing; run deploy\windows\setup.ps1 first"
} else {
    $port = (& $py -c "from brainsync.board import detect_serial_port; print(detect_serial_port())" 2>$null)
    if ($port) { Report PASS "EEG board" "$port ($(($adapters | Select-Object -First 1).FriendlyName))" }
    elseif ($adapters) { Report WARN "EEG board" "adapter present but no COM port found: $($adapters.FriendlyName -join ', ')" }
    else { Report FAIL "EEG board" "no USB serial adapter: re-plug it, use a data cable, plug straight into the laptop" }
}

# the display: Windows' reported rate, then frames measured in a PsychoPy window
$hz = (Get-CimInstance Win32_VideoController | Where-Object { $_.CurrentRefreshRate } |
    Select-Object -First 1).CurrentRefreshRate
if ($hz -and [math]::Abs($hz - $ExpectedHz) -le 1) { Report PASS "display rate" "$hz Hz reported" }
elseif ($hz) { Report FAIL "display rate" "$hz Hz reported, need ${ExpectedHz}: Settings > Display > Advanced display, choose 60 Hz and turn off Dynamic refresh rate" }
else { Report WARN "display rate" "Windows did not report a refresh rate" }

if (-not $NoWindow -and (Test-Path $py)) {
    $probe = Join-Path $env:TEMP "brainsync_frame_probe.py"
    @"
from psychopy import core, visual
win = visual.Window(fullscr=True, units='height', color=(-1, -1, -1), checkTiming=False, waitBlanking=True)
for _ in range(60):
    win.flip()
stamps = [win.flip() for _ in range(300)]
win.close()
gaps = [b - a for a, b in zip(stamps, stamps[1:])]
gaps.sort()
median = gaps[len(gaps) // 2]
late = sum(1 for g in gaps if g > 1.5 / $ExpectedHz)
print('%.2f %d %d %.1f' % (1.0 / median, late, len(gaps), 1000 * gaps[-1]))
core.quit()
"@ | Set-Content -Encoding ASCII $probe
    Write-Host "      measuring frame timing: a black full-screen window for about 6 s..."
    $out = (& $py -I $probe 2>$null | Select-Object -Last 1)
    if ($out -match "^([\d.]+) (\d+) (\d+) ([\d.]+)$") {
        $measured = [double]$Matches[1]; $late = [int]$Matches[2]; $n = [int]$Matches[3]; $worst = $Matches[4]
        $detail = "{0:N2} Hz measured, {1} of {2} frames late (worst {3} ms)" -f $measured, $late, $n, $worst
        if ([math]::Abs($measured - $ExpectedHz) -gt 1) { Report FAIL "frame timing" "$detail; the stimulus will refuse to run" }
        elseif ($late -gt 3) { Report WARN "frame timing" "$detail; close other apps, charger in, High performance" }
        else { Report PASS "frame timing" $detail }
    } else { Report FAIL "frame timing" "the PsychoPy window did not open; run setup.ps1 again" }
}

if ($results -contains "FAIL") { Write-Host "`nNO-GO: fix the FAIL lines above" -ForegroundColor Red; exit 1 }
if ($results -contains "WARN") { Write-Host "`nGO, with warnings" -ForegroundColor Yellow; exit 0 }
Write-Host "`nGO" -ForegroundColor Green
