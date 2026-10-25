<#
.SYNOPSIS
Builds the Cerelog fork of BrainFlow for Windows x64 and packages its Python binding as a
wheel. The Windows counterpart of deploy/jetson/brainflow/build_brainflow.sh.

.DESCRIPTION
An addition for Windows laptops: macOS keeps the vendored arm64 dylibs and Linux keeps
deploy/jetson/brainflow; neither changes. Same source and patches as the Jetson build
(Shared_brainflow-cerelog 932b3944, cerelog-serial-timeout.patch, setuptools-pin.patch),
plus msvc-compat.patch so current MSVC compiles it. Builds BoardController.dll and
DataHandler.dll with Visual Studio's compiler, CMake and Ninja.

Needs Visual Studio (any edition) with "Desktop development with C++", git, and a Python
for the wheel (default: py -V:3.12). Writes under -Work (default
%LOCALAPPDATA%\BrainSync\brainflow): src\ (pristine clone), build-src\ (patched copy and
build), out\ (the wheel). Prints the wheel's path last.

    powershell -ExecutionPolicy Bypass -File deploy\windows\brainflow\build_brainflow.ps1
#>
param(
    [string]$Work = (Join-Path $env:LOCALAPPDATA "BrainSync\brainflow"),
    [string]$Python = ""
)
# native tools write progress to stderr; check $LASTEXITCODE instead of stopping on it
$ErrorActionPreference = "Continue"
$RepoUrl = "https://github.com/shakimiansky/Shared_brainflow-cerelog"
$Commit = "932b3944bfe6c0f4e29a46b4a34afdeba39edd85"
$Jetson = Join-Path $PSScriptRoot "..\..\jetson\brainflow"
$Patches = @((Join-Path $Jetson "cerelog-serial-timeout.patch"),
             (Join-Path $Jetson "setuptools-pin.patch"),
             (Join-Path $PSScriptRoot "msvc-compat.patch"))
$Src = Join-Path $Work "src"
$BuildSrc = Join-Path $Work "build-src"
$Out = Join-Path $Work "out"

function Log($msg) { Write-Host "`n==> $msg" }
function Die($msg) { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

foreach ($p in $Patches) { if (-not (Test-Path $p)) { Die "missing patch $p" } }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git not found on PATH" }
if (-not $Python) {
    $Python = (& py -V:3.12 -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $Python) { Die "no Python 3.12 (install it with: py install 3.12), or pass -Python" }
}

# Visual Studio with the x64 C++ tools, and the CMake and Ninja it bundles
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { Die "Visual Studio not found; install it with 'Desktop development with C++'" }
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { Die "Visual Studio has no x64 C++ tools; add 'Desktop development with C++' in the Visual Studio Installer" }
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
$cmakeDir = Join-Path $vs "Common7\IDE\CommonExtensions\Microsoft\CMake"
$cmake = Join-Path $cmakeDir "CMake\bin\cmake.exe"
$ninja = Join-Path $cmakeDir "Ninja\ninja.exe"
foreach ($f in @($vcvars, $cmake, $ninja)) { if (-not (Test-Path $f)) { Die "missing $f" } }
Log "Visual Studio at $vs"

# 1. pristine source at the pinned commit (LF endings, as the patches expect)
if (-not (Test-Path (Join-Path $Src ".git"))) {
    New-Item -ItemType Directory -Force -Path $Src | Out-Null
    git -C $Src init -q
    git -C $Src config core.autocrlf false
    git -C $Src remote add origin $RepoUrl
}
$head = git -C $Src rev-parse -q --verify HEAD 2>$null
if (-not $head) {
    # first run, or a fetch that was interrupted: fetch again (only this commit)
    Log "fetching $RepoUrl at $($Commit.Substring(0, 8)) into $Src (slow on a weak network)"
    git -C $Src fetch --depth 1 origin $Commit
    if ($LASTEXITCODE -ne 0) { Die "could not fetch $Commit from $RepoUrl" }
    git -C $Src checkout -q --detach FETCH_HEAD
    $head = git -C $Src rev-parse HEAD
}
if ($head -ne $Commit) { Die "$Src is at $head, expected $Commit" }
if (git -C $Src status --porcelain) { Die "$Src has local changes; it must stay pristine" }

# 2. fresh checkout of exactly that commit, then the patches (the clone is never touched).
# A detached worktree of the clone, so git apply resolves paths inside it.
Log "checking out $($Commit.Substring(0, 8)) into $BuildSrc and applying patches"
if (Test-Path $BuildSrc) { Remove-Item -Recurse -Force $BuildSrc }
git -C $Src worktree prune
git -C $Src worktree add -q --detach --force $BuildSrc $Commit
if ($LASTEXITCODE -ne 0) { Die "could not check out $Commit into $BuildSrc" }
foreach ($p in $Patches) {
    git -C $BuildSrc apply --whitespace=nowarn $p
    if ($LASTEXITCODE -ne 0) { Die "could not apply $p" }
    Write-Host "applied $(Split-Path $p -Leaf)"
}
$cerelog = Get-Content -Raw (Join-Path $BuildSrc "src\board_controller\cerelog\cerelog.cpp")
if ($cerelog -notmatch "set_serial_port_settings \(1000, false\)") { Die "serial timeout patch missing after apply" }

# 3. native libraries. Ninja Multi-Config puts them in compiled\Release, where the fork's
# POST_BUILD steps expect them (they copy each DLL into python_package\brainflow\lib).
Log "configuring and building BoardController and DataHandler"
$bat = Join-Path $env:TEMP "brainsync_build_brainflow.bat"
# UTF-8 without a BOM plus chcp 65001, so a profile path with accents survives
$batText = @"
@echo off
chcp 65001 >nul
call "$vcvars" >nul || exit /b 1
"$cmake" -S "$BuildSrc" -B "$BuildSrc\build" -G "Ninja Multi-Config" -DCMAKE_MAKE_PROGRAM="$ninja" -DBRAINFLOW_VERSION=0.0.1 -DBUILD_SYNCHRONI_SDK=OFF -DBUILD_OYMOTION_SDK=OFF -DBUILD_BLUETOOTH=OFF -DBUILD_BLE=OFF -DCMAKE_POLICY_VERSION_MINIMUM=3.5 || exit /b 2
"$cmake" --build "$BuildSrc\build" --config Release --target BoardController DataHandler || exit /b 3
"@
[IO.File]::WriteAllText($bat, $batText, (New-Object Text.UTF8Encoding $false))
cmd /c "`"$bat`""
if ($LASTEXITCODE -ne 0) { Die "build failed (exit $LASTEXITCODE); see the output above" }
$lib = Join-Path $BuildSrc "python_package\brainflow\lib"
New-Item -ItemType Directory -Force -Path $lib | Out-Null
foreach ($name in @("BoardController.dll", "DataHandler.dll")) {
    $dll = @("compiled\Release\$name", "compiled\$name") | ForEach-Object { Join-Path $BuildSrc $_ } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $dll) { Die "$name not found under $BuildSrc\compiled" }
    Copy-Item -Force $dll $lib
}
Log "native libraries"
Get-ChildItem $lib | Format-Table Name, Length -AutoSize | Out-Host

# 4. wheel. setup.py has no extensions, so setuptools tags it py3-none-any; retag it for
# Windows x64 because it ships DLLs.
Log "building the wheel with $Python"
$venv = Join-Path $BuildSrc ".wheel-venv"
& $Python -m venv $venv
$vpy = Join-Path $venv "Scripts\python.exe"
& $vpy -m pip install --quiet --disable-pip-version-check "setuptools<81" "wheel>=0.42"
if ($LASTEXITCODE -ne 0) { Die "could not install setuptools and wheel into $venv" }
$dist = Join-Path $BuildSrc "dist"
& $vpy -m pip wheel --quiet --disable-pip-version-check --no-build-isolation --no-deps -w $dist (Join-Path $BuildSrc "python_package")
if ($LASTEXITCODE -ne 0) { Die "pip wheel failed" }
$any = Get-ChildItem (Join-Path $dist "brainflow-*-py3-none-any.whl") | Select-Object -First 1
& $vpy -m wheel tags --remove --python-tag py3 --abi-tag none --platform-tag win_amd64 $any.FullName | Out-Null
if ($LASTEXITCODE -ne 0) { Die "wheel tags failed" }
New-Item -ItemType Directory -Force -Path $Out | Out-Null
Remove-Item -Force (Join-Path $Out "brainflow-*.whl") -ErrorAction SilentlyContinue
$wheel = Get-ChildItem (Join-Path $dist "brainflow-*-py3-none-win_amd64.whl") | Select-Object -First 1
Copy-Item -Force $wheel.FullName $Out
$wheelPath = Join-Path $Out $wheel.Name
Log "wheel: $wheelPath"
Write-Output $wheelPath
