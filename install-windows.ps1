#Requires -Version 5.1
<#
.SYNOPSIS
    Native PowerShell installer for DVD Player on Windows.

.DESCRIPTION
    Finds uv (installing it with winget when missing), lets uv provide a
    managed CPython 3.13, then runs scripts/install.py --platform windows
    through `uv run` and launches dvd-player.exe. No system Python is needed
    and no administrator rights are required or requested.

.PARAMETER Ask
    Prompt before install.py's own dependency steps (uv/winget stays silent).

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

Write-Host ''
Write-Host 'NOTICE: Windows support is verified statically, not on real hardware; encrypted-DVD playback depends on the libmpv build; Start Menu indexing may lag.'
Write-Host ''

function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return [string]$command.Source
    }
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:LOCALAPPDATA) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'))
    }
    if ($env:USERPROFILE) {
        $candidates.Add((Join-Path $env:USERPROFILE '.local\bin\uv.exe'))
        $candidates.Add((Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'))
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Install-Uv {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Err 'uv is missing and winget is not available.'
        Write-Err 'Install uv from https://docs.astral.sh/uv/ and re-run.'
        return $null
    }
    & winget install --id astral-sh.uv --exact --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        Write-Err 'winget could not install uv.'
        return $null
    }
    return (Find-Uv)
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
    $uv = Find-Uv
    if ($null -eq $uv) {
        $uv = Install-Uv
    }
    if ($null -eq $uv) {
        Write-Err 'uv was not found. Install it from https://docs.astral.sh/uv/ and re-run.'
        exit 1
    }
    if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) {
        Write-Err "the selected uv is not usable: $uv"
        exit 1
    }

    $installer = Join-Path $Repo 'scripts/install.py'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        Write-Err "installer not found: $installer"
        exit 1
    }

    & $uv python install 3.13
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    $installArgs = New-Object System.Collections.Generic.List[string]
    $installArgs.Add('run')
    $installArgs.Add('--python')
    $installArgs.Add('3.13')
    $installArgs.Add($installer)
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
    & $uv @finalArgs
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
        $process = Start-Process -FilePath $exe -WorkingDirectory $Repo -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            Write-Err "dvd-player exited with code $($process.ExitCode)."
            $log = Join-Path $env:LOCALAPPDATA 'dvd-player\dvd-player.log'
            Write-Err "See the UI log: $log"
            exit $process.ExitCode
        }
    }

    $Error.Clear()
    exit 0
} catch {
    Write-Err $_.Exception.Message
    exit 1
}
