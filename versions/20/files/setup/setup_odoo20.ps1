# Odoo 20 environment setup -- same layout as C:\odoo\odoo19.
# Safe to run again: existing clones and the venv are kept, dependencies are re-checked.
#
#   powershell -ExecutionPolicy Bypass -File C:\odoo\odoo20\tools\setup\setup_odoo20.ps1

# Continue, not Stop: git and pip write progress to stderr, which PowerShell would
# otherwise treat as a failure. Every step checks $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$Root   = "C:\odoo\odoo20"
$Branch = "20.0"

function Clone-Repo($Name, $Target, [switch]$Optional) {
    if (Test-Path (Join-Path $Target ".git\HEAD")) {
        $ok = git -C $Target rev-parse --verify HEAD 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Host "[skip] $Name already cloned at $Target"; return }
    }
    # Large shallow clones over Windows can drop mid-transfer ("invalid index-pack output").
    # HTTP/1.1 is steadier for that; a failed attempt leaves a partial folder, which is removed before retrying.
    for ($try = 1; $try -le 3; $try++) {
        if (Test-Path $Target) { Remove-Item -Recurse -Force $Target }
        Write-Host "[clone] odoo/$Name ($Branch) -> $Target  (attempt $try/3)"
        git -c http.version=HTTP/1.1 clone https://github.com/odoo/$Name.git --depth 1 --branch $Branch --single-branch $Target
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 5
    }
    if (Test-Path $Target) { Remove-Item -Recurse -Force $Target }
    if ($Optional) { Write-Warning "Could not clone odoo/$Name (no access?). Skipped."; return }
    throw "git clone of odoo/$Name failed 3 times"
}

# 1. Python 3.12 - 3.14 (odoo/release.py). 3.13 first: every pinned package has a Windows wheel there.
$Py = $null
foreach ($v in "3.13", "3.12", "3.14") {
    try { & py -$v -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $Py = $v; break } } catch {}
}
$PyExe = $null
if (-not $Py) {
    foreach ($v in "313", "312", "314") {
        $c = "$env:LOCALAPPDATA\Programs\Python\Python$v\python.exe"
        if (Test-Path $c) { $PyExe = $c; $Py = "direct"; break }
    }
}
if (-not $Py) { throw "Odoo 20 needs Python 3.12, 3.13 or 3.14 (py launcher). Install one and run again." }
Write-Host "[python] using $Py"

# 2. Sources
Clone-Repo "odoo"          "$Root\odoo"
Clone-Repo "design-themes" "$Root\design-themes"
Clone-Repo "enterprise"    "$Root\enterprise" -Optional   # private repo: uses your GitHub login

# 3. Virtual environment
if (-not (Test-Path "$Root\venv\Scripts\python.exe")) {
    Write-Host "[venv] creating $Root\venv"
    if ($PyExe) { & $PyExe -m venv "$Root\venv" } else { & py -$Py -m venv "$Root\venv" }
}
$VPy = "$Root\venv\Scripts\python.exe"

# 4. Dependencies + dev tools (same set as 17/18/19)
& $VPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed (venv not created?)" }
& $VPy -m pip install -r "$Root\odoo\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed" }

# Register Odoo itself in the venv (editable): `import odoo` and the `odoo` command
# then point at C:\odoo\odoo20\odoo, and a git pull there is picked up without reinstalling.
Push-Location "$Root\odoo"
& $VPy -m pip install -e .
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { throw "pip install -e . failed" }
& $VPy -m pip install black isort pylint pylint-odoo flake8

& $VPy "$Root\odoo\odoo-bin" --version
& $VPy -c "import odoo, odoo.release as r; print('odoo package:', odoo.__path__[0], r.version)"
Write-Host ""
Write-Host "Done. Next: set real passwords in $Root\odoo.conf and create the PostgreSQL role 'odoo20'."
