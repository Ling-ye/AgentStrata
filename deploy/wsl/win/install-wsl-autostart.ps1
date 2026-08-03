#!/usr/bin/env pwsh
[CmdletBinding(DefaultParameterSetName = 'Install')]
param(
    [string]$Distro = $(if ($env:CHATCOPILOT_WSL_DISTRO) { $env:CHATCOPILOT_WSL_DISTRO } else { 'Ubuntu-22.04' }),
    [string]$RunValueName = 'ChatCopilotStartWSL',
    [Parameter(ParameterSetName = 'Status')]
    [switch]$Status,
    [Parameter(ParameterSetName = 'Probe')]
    [switch]$Probe,
    [Parameter(ParameterSetName = 'Uninstall')]
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$WslExe = Join-Path $env:SystemRoot 'System32\wsl.exe'
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$LauncherRoot = Join-Path $env:LOCALAPPDATA 'ChatCopilot'
$LauncherPath = Join-Path $LauncherRoot 'Start-WSL.ps1'
$StatusPath = Join-Path $LauncherRoot 'wsl-autostart-status.json'
$LegacyTaskName = 'ChatCopilot-Start-WSL'

function Get-RunCommand {
    $Properties = Get-ItemProperty -LiteralPath $RunKey -ErrorAction SilentlyContinue
    if (-not $Properties) {
        return $null
    }
    $Property = $Properties.PSObject.Properties[$RunValueName]
    if (-not $Property) {
        return $null
    }
    return $Property.Value
}

function Remove-LegacyTask {
    $Task = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
    if ($Task) {
        Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false
        Write-Output "[OK] removed superseded task: $LegacyTaskName"
    }
}

if ($Status) {
    $Command = Get-RunCommand
    if (-not $Command -or -not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
        Write-Output "[MISSING] run_value=$RunValueName launcher=$LauncherPath"
        exit 1
    }
    Write-Output "[OK] run_value=$RunValueName"
    Write-Output "     command=$Command"
    if (Test-Path -LiteralPath $StatusPath -PathType Leaf) {
        $Last = Get-Content -LiteralPath $StatusPath -Raw | ConvertFrom-Json
        Write-Output ("     last_wake={0} distro={1} exit_code={2}" -f
            $Last.time, $Last.distro, $Last.exit_code)
    }
    exit 0
}

if ($Uninstall) {
    if (Get-RunCommand) {
        Remove-ItemProperty -LiteralPath $RunKey -Name $RunValueName
        Write-Output "[OK] removed run value: $RunValueName"
    }
    foreach ($Path in @($LauncherPath, $StatusPath)) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            Remove-Item -LiteralPath $Path -Force
        }
    }
    Remove-LegacyTask
    Write-Output "[OK] WSL autostart is absent"
    exit 0
}

if ($Probe) {
    if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
        throw "launcher is not installed: $LauncherPath"
    }
    & $PowerShellExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $LauncherPath
    exit $LASTEXITCODE
}

if ($Distro -notmatch '^[A-Za-z0-9._-]+$') {
    throw "invalid WSL distro name: $Distro"
}
if (-not (Test-Path -LiteralPath $WslExe -PathType Leaf)) {
    throw "wsl.exe not found: $WslExe"
}

$KnownDistros = @(& $WslExe --list --quiet) |
    ForEach-Object { ($_ -replace [char]0, '').Trim() } |
    Where-Object { $_ }
if ($Distro -notin $KnownDistros) {
    throw "WSL distro is not registered for the current user: $Distro"
}

New-Item -ItemType Directory -Path $LauncherRoot -Force | Out-Null
$QuotedDistro = $Distro.Replace("'", "''")
$QuotedStatusPath = $StatusPath.Replace("'", "''")
$Launcher = @"
`$ErrorActionPreference = 'Continue'
& "`$env:SystemRoot\System32\wsl.exe" -d '$QuotedDistro' --exec /bin/true
`$ExitCode = `$LASTEXITCODE
@{
    time = (Get-Date).ToString('o')
    distro = '$QuotedDistro'
    exit_code = `$ExitCode
} | ConvertTo-Json -Compress | Set-Content -LiteralPath '$QuotedStatusPath' -Encoding UTF8
exit `$ExitCode
"@
Set-Content -LiteralPath $LauncherPath -Value $Launcher -Encoding UTF8

New-Item -Path $RunKey -Force | Out-Null
$RunCommand = '"{0}" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{1}"' -f `
    $PowerShellExe, $LauncherPath
New-ItemProperty `
    -LiteralPath $RunKey `
    -Name $RunValueName `
    -Value $RunCommand `
    -PropertyType String `
    -Force | Out-Null
Remove-LegacyTask

Write-Output "[OK] installed HKCU Run launcher: $RunValueName"
Write-Output "     distro=$Distro launcher=$LauncherPath"
