#Requires -Version 5.1
<#
.SYNOPSIS
    Native PowerShell installer for DVD Player on Windows.

.DESCRIPTION
    Finds Python 3.12+ (installing it with winget when missing), then runs
    scripts/install.py --platform windows and launches dvd-player.exe.
    No administrator rights are required or requested.

.PARAMETER Ask
    Prompt before installing missing Python, uv or mpv.

.PARAMETER Yes
    Accepted for compatibility; unattended installs are the default.

.PARAMETER Check
    Verify only; make no changes.

.PARAMETER Uninstall
    Remove the dvd-player tool and its OS entry point.

.PARAMETER Purge
    With -Uninstall, also delete config/state.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1

.EXAMPLE
    .\install-windows.ps1 -Uninstall -Purge
#>
[CmdletBinding()]
param(
    [switch]$Ask,
    [switch]$Yes,
    [switch]$Check,
    [switch]$Uninstall,
    [switch]$Purge,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Error.Clear()

function Write-Err {
    param([string]$Message)
    [Console]::Error.WriteLine("install-windows.ps1: $Message")
}

function Test-PythonExe {
    param(
        [string]$File,
        [string[]]$Prefix
    )
    $code = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
    try {
        & $File @Prefix -c $code 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = @()
    $candidates += ,@{ File = 'py'; Prefix = @('-3') }
    $candidates += ,@{ File = 'python'; Prefix = @() }
    if ($env:LOCALAPPDATA) {
        foreach ($version in @('312', '313')) {
            $exe = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
            if (Test-Path -LiteralPath $exe -PathType Leaf) {
                $candidates += ,@{ File = $exe; Prefix = @() }
            }
        }
    }
    foreach ($candidate in $candidates) {
        $file = [string]$candidate.File
        $prefix = @($candidate.Prefix)
        if ($file -eq 'py' -or $file -eq 'python') {
            if (-not (Get-Command $file -ErrorAction SilentlyContinue)) {
                continue
            }
        } elseif (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            continue
        }
        if (Test-PythonExe -File $file -Prefix $prefix) {
            return @{ File = $file; Prefix = $prefix }
        }
    }
    return $null
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Err 'Python 3.12+ is missing and winget is not available.'
        Write-Err 'Install Python 3.12+ from https://www.python.org/downloads/ and re-run.'
        return $null
    }
    if ($Ask) {
        $reply = Read-Host 'Python 3.12+ is missing. Install it now with winget? [y/N]'
        if ($reply -notmatch '^(?i:y|yes)$') {
            Write-Err 'Python installation declined; nothing changed.'
            return $null
        }
    }
    & winget install --id Python.Python.3.12 -e --source winget --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Err 'winget could not install Python 3.12+.'
        return $null
    }
    return (Find-Python)
}

# Accept cmd-style flags (--uninstall, --ask, ...) forwarded by install-windows.cmd.
$Passthrough = New-Object System.Collections.Generic.List[string]
$index = 0
while ($index -lt $Rest.Count) {
    $arg = [string]$Rest[$index]
    switch ($arg.ToLowerInvariant()) {
        '--ask' { $Ask = $true }
        '-ask' { $Ask = $true }
        '--yes' { $Yes = $true }
        '-yes' { $Yes = $true }
        '--check' { $Check = $true; $Uninstall = $false }
        '-check' { $Check = $true; $Uninstall = $false }
        '--install' { $Check = $false; $Uninstall = $false }
        '-install' { $Check = $false; $Uninstall = $false }
        '--uninstall' { $Uninstall = $true; $Check = $false }
        '-uninstall' { $Uninstall = $true; $Check = $false }
        '--purge' { $Purge = $true }
        '-purge' { $Purge = $true }
        '--offline' {
            if ($index + 1 -ge $Rest.Count) {
                throw '--offline requires a directory argument.'
            }
            $Passthrough.Add('--offline')
            $Passthrough.Add([string]$Rest[$index + 1])
            $index++
        }
        '--dvdcss' {
            if ($index + 1 -ge $Rest.Count) {
                throw '--dvdcss requires a file argument.'
            }
            $Passthrough.Add('--dvdcss')
            $Passthrough.Add([string]$Rest[$index + 1])
            $index++
        }
        '--no-sync' { $Passthrough.Add('--no-sync') }
        '-no-sync' { $Passthrough.Add('--no-sync') }
        '--pause' { $Passthrough.Add('--pause') }
        default { $Passthrough.Add($arg) }
    }
    $index++
}

$Repo = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Repo)) {
    Write-Err 'cannot locate the repository (missing $PSScriptRoot).'
    exit 1
}

try {
    $python = Find-Python
    if ($null -eq $python) {
        $python = Install-Python
    }
    if ($null -eq $python) {
        Write-Err 'Python 3.12+ was not found.'
        exit 1
    }

    # Verify the interpreter again before trusting it.
    if (-not (Test-PythonExe -File ([string]$python.File) -Prefix @($python.Prefix))) {
        Write-Err 'the selected Python is older than 3.12.'
        exit 1
    }
    $pyFile = [string]$python.File
    $pyPrefix = @($python.Prefix)

    $installer = Join-Path $Repo 'scripts/install.py'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        Write-Err "installer not found: $installer"
        exit 1
    }

    $installArgs = New-Object System.Collections.Generic.List[string]
    $installArgs.Add('--platform')
    $installArgs.Add('windows')
    if ($Uninstall) {
        $installArgs.Add('--uninstall')
        if ($Purge) {
            $installArgs.Add('--purge')
        }
    } elseif ($Check) {
        $installArgs.Add('--check')
    } else {
        $installArgs.Add('--install')
    }
    if ($Ask) {
        $installArgs.Add('--ask')
    }
    if ($Yes) {
        $installArgs.Add('--yes')
    }
    foreach ($extra in $Passthrough) {
        $installArgs.Add($extra)
    }

    $finalArgs = $installArgs.ToArray()
    & $pyFile @pyPrefix $installer @finalArgs
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        exit $code
    }

    if (-not $Check -and -not $Uninstall) {
        $exe = Join-Path $env:USERPROFILE '.local\bin\dvd-player.exe'
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
            Write-Err "dvd-player.exe was not found after install: $exe"
            Write-Err "Run: uv tool install --force `"$Repo`""
            exit 1
        }
        $lib = Join-Path $env:LOCALAPPDATA 'dvd-player\lib'
        $env:PATH = "$lib;$env:PATH"
        Start-Process -FilePath $exe -WorkingDirectory $Repo | Out-Null
    }

    $Error.Clear()
    exit 0
} catch {
    Write-Err $_.Exception.Message
    exit 1
}
