#!/usr/bin/env pwsh
# Install or refresh the AgentStrata ccwsl helper in the current PowerShell profile.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProfilePath = $PROFILE,
    [string]$Distro = $(if ($env:CHATCOPILOT_WSL_DISTRO) { $env:CHATCOPILOT_WSL_DISTRO } else { 'Ubuntu-22.04' }),
    [string]$Repo = $(if ($env:CHATCOPILOT_WSL_REPO) { $env:CHATCOPILOT_WSL_REPO } else { '~/ChatCopilot' })
)

$ErrorActionPreference = 'Stop'

$BlockStart = '# >>> ChatCopilot ccwsl >>>'
$BlockEnd = '# <<< ChatCopilot ccwsl <<<'
$EscapedDistro = $Distro.Replace("'", "''")
$EscapedRepo = $Repo.Replace("'", "''")

$Block = @"
# >>> ChatCopilot ccwsl >>>
function ccwsl {
    [CmdletBinding()]
    param([Parameter(ValueFromRemainingArguments = `$true)][string[]]`$Command)

    `$Distro = if (`$env:CHATCOPILOT_WSL_DISTRO) { `$env:CHATCOPILOT_WSL_DISTRO } else { '$EscapedDistro' }
    `$Repo = if (`$env:CHATCOPILOT_WSL_REPO) { `$env:CHATCOPILOT_WSL_REPO } else { '$EscapedRepo' }

    function ConvertTo-ChatCopilotBashSingleQuoted([string]`$Value) {
        `$SingleQuote = [char]39
        `$DoubleQuote = [char]34
        `$EscapedSingleQuote = -join @(`$SingleQuote, `$DoubleQuote, `$SingleQuote, `$DoubleQuote, `$SingleQuote)
        return `$SingleQuote + (`$Value -replace [regex]::Escape([string]`$SingleQuote), `$EscapedSingleQuote) + `$SingleQuote
    }

    `$QuotedRepo = ConvertTo-ChatCopilotBashSingleQuoted `$Repo
    if (-not `$Command -or `$Command.Count -eq 0) {
        `$BashCommand = 'cd {0} && exec bash -l' -f `$QuotedRepo
    }
    elseif (`$Command.Count -eq 1) {
        `$BashCommand = 'cd {0} && {1}' -f `$QuotedRepo, `$Command[0]
    }
    else {
        `$QuotedCommand = (`$Command | ForEach-Object { ConvertTo-ChatCopilotBashSingleQuoted `$_ }) -join ' '
        `$BashCommand = 'cd {0} && {1}' -f `$QuotedRepo, `$QuotedCommand
    }

    wsl -d `$Distro --exec bash -lc `$BashCommand
}
# <<< ChatCopilot ccwsl <<<
"@

$ProfileDir = Split-Path -Parent $ProfilePath
if ($ProfileDir -and -not (Test-Path -LiteralPath $ProfileDir)) {
    if ($PSCmdlet.ShouldProcess($ProfileDir, 'Create PowerShell profile directory')) {
        New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null
    }
}

$Existing = ''
if (Test-Path -LiteralPath $ProfilePath) {
    $Existing = Get-Content -LiteralPath $ProfilePath -Raw
}

$Pattern = '(?s)\r?\n?' + [regex]::Escape($BlockStart) + '.*?' + [regex]::Escape($BlockEnd) + '\r?\n?'
$Updated = [regex]::Replace($Existing, $Pattern, '')
$Updated = $Updated.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $Block + [Environment]::NewLine

if ($PSCmdlet.ShouldProcess($ProfilePath, 'Install ChatCopilot ccwsl profile function')) {
    Set-Content -LiteralPath $ProfilePath -Value $Updated -Encoding UTF8
}

Write-Host ('Installed ccwsl in {0}' -f $ProfilePath) -ForegroundColor Green
Write-Host 'Open a new PowerShell session or run: . $PROFILE' -ForegroundColor Cyan
Write-Host ('Defaults: distro={0}; repo={1}' -f $Distro, $Repo) -ForegroundColor Cyan
Write-Host 'Override with CHATCOPILOT_WSL_DISTRO / CHATCOPILOT_WSL_REPO or pass -Distro / -Repo during install.' -ForegroundColor Cyan
