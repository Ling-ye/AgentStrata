param([Parameter(Mandatory)][string]$Bot, [Parameter(Mandatory)][string]$Installer, [switch]$ServicesReady)
$ErrorActionPreference = 'Stop'
if ($Bot -notmatch '^[a-z][a-z0-9-]*$') { throw '应用标识无效。' }
$digest = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
$dir = Join-Path $env:LOCALAPPDATA "AgentStrata\Apps\$Bot\receipts"
[IO.Directory]::CreateDirectory($dir) | Out-Null
$file = Join-Path $dir "$digest.json"
$temp = Join-Path $dir ([guid]::NewGuid().ToString('N') + '.tmp')
$data = [ordered]@{format='agentstrata-install/v1'; application=$Bot; platform='windows-x64'; sha256=$digest;
    completed_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); services_ready=$ServicesReady.IsPresent}
try {
    [IO.File]::WriteAllText($temp, ($data | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($file)) { [IO.File]::Replace($temp, $file, [System.Management.Automation.Language.NullString]::Value) } else { [IO.File]::Move($temp, $file) }
} finally {
    if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) }
}
Write-Host "接收方安装记录 $file"
