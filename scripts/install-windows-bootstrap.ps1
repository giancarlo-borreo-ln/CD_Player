# Bootstrap Python 3.12+ on Windows for install-windows.cmd.
# Consent is explicit: never installs silently (unless -AssumeYes is passed).
param(
    [string]$Repo = "",
    [string]$OutFile = "",
    [switch]$AssumeYes
)
$ErrorActionPreference = "Stop"

function Test-Python([string]$exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    try {
        $version = & $exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        return ([version]$version -ge [version]"3.12")
    } catch { return $false }
}

function Find-Python {
    $candidates = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $exe = & py -3 -c "import sys;print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0) { $candidates += $exe }
        } catch {}
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $candidates += $python.Source }
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")
    }
    foreach ($candidate in $candidates) {
        if (Test-Python $candidate) { return $candidate }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Error "Python 3.12+ is missing and winget is unavailable. Install Python 3.12+ from python.org, then re-run."
        exit 1
    }
    $approved = $AssumeYes.IsPresent
    if (-not $approved) {
        $answer = Read-Host "Python 3.12+ is missing. Install it now with winget? [y/N]"
        $approved = $answer -match "^(y|yes)$"
    }
    if (-not $approved) {
        Write-Error "Python 3.12+ is required; install it and re-run."
        exit 1
    }
    $wingetArgs = @(
        "install", "--id", "Python.Python.3.12", "--silent", "--exact",
        "--accept-package-agreements", "--accept-source-agreements"
    )
    & winget @wingetArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "winget could not install Python 3.12+ (exit $LASTEXITCODE)."
        exit 1
    }
    $python = Find-Python
    if (-not $python) {
        Write-Error "Python was installed but is not visible yet. Open a new terminal and re-run."
        exit 1
    }
}

if ($OutFile) {
    Set-Content -LiteralPath $OutFile -Value $python -Encoding ASCII -NoNewline
} else {
    Write-Output $python
}
exit 0
