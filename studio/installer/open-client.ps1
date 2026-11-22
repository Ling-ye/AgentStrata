$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$config = Get-Content -LiteralPath 'install.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$address = & docker.exe compose -p ("as-" + $config.application) -f compose.yaml port $config.entry.service $config.entry.port
if ($LASTEXITCODE -ne 0 -or !$address) { throw '请先启动应用服务。' }
$env:AGENTSTRATA_SERVICE_URL = "http://$address/"
if ($config.clients.'windows-x64') {
    $client = Join-Path $PSScriptRoot $config.clients.'windows-x64'
    Start-Process -FilePath $client -WorkingDirectory (Split-Path -LiteralPath $client)
} else {
    Start-Process $env:AGENTSTRATA_SERVICE_URL
}
