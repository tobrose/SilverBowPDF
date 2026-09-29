# Build SilverbowPDF into a single-click Windows installer.
#
#   .\packaging\build.ps1              full build
#   .\packaging\build.ps1 -SkipTests   skip the test gate (don't)
#   .\packaging\build.ps1 -NoInstaller  stop after PyInstaller
#
# Run from the repo root in PowerShell.

[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$NoInstaller
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($msg) { Write-Host "`n=== $msg" -ForegroundColor Cyan }

# --- venv ------------------------------------------------------------------
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Step "Creating virtualenv"
    python -m venv .venv
}

Step "Installing dependencies"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -e ".[dev,build]" --quiet

# --- test gate -------------------------------------------------------------
# The coordinate math is validated against pdfium here. Shipping past a failure
# means shipping an installer whose annotations land in the wrong place.
if (-not $SkipTests) {
    Step "Running tests"
    foreach ($test in Get-ChildItem tests\test_*.py) {
        & $venvPython $test.FullName
        if ($LASTEXITCODE -ne 0) { throw "$($test.Name) failed. Not building." }
    }
}

# --- clean -----------------------------------------------------------------
Step "Cleaning previous build"
foreach ($dir in @("build", "dist", "packaging\out")) {
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
}

# --- freeze ----------------------------------------------------------------
Step "Running PyInstaller"
& $venvPython -m PyInstaller packaging\pdfapp.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$exe = "dist\SilverbowPDF\SilverbowPDF.exe"
if (-not (Test-Path $exe)) { throw "Expected $exe, not found." }

$sizeMB = [math]::Round((Get-ChildItem dist\SilverbowPDF -Recurse |
    Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "Frozen bundle: $sizeMB MB" -ForegroundColor Green

# Smoke test: does the frozen app import everything it needs? This catches the
# pypdfium2 version.json problem, which otherwise only surfaces on a machine
# that has no Python installed -- i.e. a user's machine, after you ship.
Step "Smoke testing the frozen bundle"
$proc = Start-Process -FilePath $exe -PassThru
Start-Sleep -Seconds 6
if ($proc.HasExited) {
    throw "Frozen app exited immediately (code $($proc.ExitCode)). Rebuild the spec with console=True in packaging\pdfapp.spec to see the traceback."
}
Stop-Process -Id $proc.Id -Force
Write-Host "App launched and stayed up." -ForegroundColor Green

# Staying up proves the imports; the self-test proves the DLLs and data files
# behind them: render through pdfium, edit and save through pikepdf, reopen,
# open an encrypted file. Same command works on any machine after install.
Step "Self-testing the frozen bundle"
$log = Join-Path $root "dist\selftest.log"
$proc = Start-Process -FilePath $exe -ArgumentList "--self-test", "`"$log`"" -PassThru -Wait
Get-Content $log | Write-Host
if ($proc.ExitCode -ne 0) { throw "Frozen self-test failed (exit $($proc.ExitCode)). See $log." }

if ($NoInstaller) { Step "Done (installer skipped)"; exit 0 }

# --- installer -------------------------------------------------------------
Step "Building installer"
$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"  # winget's per-user install
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Write-Warning "Inno Setup 6 not found. Install it (winget install JRSoftware.InnoSetup) and re-run."
    exit 1
}

$version = & $venvPython -c "import pdfapp; print(pdfapp.__version__)"
Write-Host "Version $version"
& $iscc "/DAppVersion=$version" packaging\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }

$setup = Get-ChildItem packaging\out\*.exe | Select-Object -First 1
$setupMB = [math]::Round($setup.Length / 1MB, 1)
Write-Host "`nInstaller: $($setup.FullName) ($setupMB MB)" -ForegroundColor Green
Write-Host "Silent install: $($setup.Name) /VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -ForegroundColor DarkGray
