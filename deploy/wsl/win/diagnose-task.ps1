#!/usr/bin/env pwsh
# Generate a targeted, redacted task diagnostic bundle from the WSL control repo.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(task|job)_\d{8}_\d{6}_[0-9a-fA-F]{8}$')]
    [string]$Id,
    [string]$Distro = ""
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$WinRepo = (Get-Item "$ScriptRoot\..\..\..").FullName
$Timestamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$Output = Join-Path $WinRepo "_wsl_debug\task-diagnostics\${Id}_${Timestamp}"

function ConvertTo-WslPath([string]$Path) {
    if ($Path -match '^([A-Za-z]):[\\/](.*)$') {
        return "/mnt/$($matches[1].ToLower())/$($matches[2] -replace '\\', '/')"
    }
    throw "Cannot convert path to WSL: $Path"
}

$WslOutput = ConvertTo-WslPath $Output
$WslArgs = @()
if ($Distro) { $WslArgs += @('-d', $Distro) }
$Command = "cd ~/ChatCopilot && PYTHONPATH=src .venv/bin/python -m console.control diagnose --id '$Id' --out '$WslOutput' --json"

Write-Host "[diagnose] collecting $Id ..." -ForegroundColor Cyan
& wsl @WslArgs bash -lc $Command
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[OK] $Output" -ForegroundColor Green
Write-Output $Output
