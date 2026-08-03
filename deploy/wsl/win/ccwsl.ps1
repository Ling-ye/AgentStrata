#!/usr/bin/env pwsh
# Run AgentStrata commands inside WSL without using a Windows UNC repo cwd.
[CmdletBinding()]
param(
    [string]$Distro = $(if ($env:CHATCOPILOT_WSL_DISTRO) { $env:CHATCOPILOT_WSL_DISTRO } else { 'Ubuntu-22.04' }),
    [string]$Repo = $(if ($env:CHATCOPILOT_WSL_REPO) { $env:CHATCOPILOT_WSL_REPO } else { '~/ChatCopilot' }),
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Command
)

$ErrorActionPreference = 'Stop'

function ConvertTo-BashSingleQuoted([string]$Value) {
    $SingleQuote = [char]39
    $DoubleQuote = [char]34
    $EscapedSingleQuote = -join @($SingleQuote, $DoubleQuote, $SingleQuote, $DoubleQuote, $SingleQuote)
    return $SingleQuote + ($Value -replace [regex]::Escape([string]$SingleQuote), $EscapedSingleQuote) + $SingleQuote
}

$QuotedRepo = ConvertTo-BashSingleQuoted $Repo

if (-not $Command -or $Command.Count -eq 0) {
    $BashCommand = 'cd {0} && exec bash -l' -f $QuotedRepo
}
elseif ($Command.Count -eq 1) {
    $BashCommand = 'cd {0} && {1}' -f $QuotedRepo, $Command[0]
}
else {
    $QuotedCommand = ($Command | ForEach-Object { ConvertTo-BashSingleQuoted $_ }) -join ' '
    $BashCommand = 'cd {0} && {1}' -f $QuotedRepo, $QuotedCommand
}

& wsl -d $Distro --exec bash -lc $BashCommand
exit $LASTEXITCODE
