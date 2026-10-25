param(
  [string]$SourceRoot = (Split-Path $PSScriptRoot -Parent),
  [string]$BuildRoot = (Join-Path $env:LOCALAPPDATA 'AgentStrata\build\live2d-desktop')
)
$ErrorActionPreference = 'Stop'
if ($BuildRoot.StartsWith('\\')) { throw 'BuildRoot must be on a native Windows drive, not UNC.' }
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
# Explicit public inputs. Do not copy a checkout cache, userData, WSL config or character assets.
foreach ($name in @('package.json','package-lock.json','tsconfig.json')) {
  Copy-Item (Join-Path $SourceRoot $name) (Join-Path $BuildRoot $name) -Force
}
foreach ($name in @('src','ui','scripts','test','licenses')) {
  New-Item -ItemType Directory -Path (Join-Path $BuildRoot $name) -Force | Out-Null
  Copy-Item (Join-Path $SourceRoot "$name\*") (Join-Path $BuildRoot $name) -Recurse -Force
}
$RepoRoot = Split-Path (Split-Path $SourceRoot -Parent) -Parent
foreach ($name in @('LICENSE','COPYRIGHT')) {
  Copy-Item (Join-Path $RepoRoot $name) (Join-Path $BuildRoot "licenses\$name") -Force
}
foreach ($name in @('Open-LLM-VTuber-LICENSE.txt','Open-LLM-VTuber-Web-LICENSE.txt')) {
  Copy-Item (Join-Path $RepoRoot "third_party\$name") (Join-Path $BuildRoot "licenses\$name") -Force
}
Push-Location $BuildRoot
try {
  $Node = (Get-Command node.exe -ErrorAction Stop).Source
  $Runner = Join-Path $BuildRoot 'scripts\build-native.cjs'
  $process = Start-Process -FilePath $Node -ArgumentList ('"' + $Runner + '"') -WorkingDirectory $BuildRoot -NoNewWindow -Wait -PassThru
  if ($process.ExitCode -ne 0) { throw "Desktop build failed: $($process.ExitCode)" }
  Get-ChildItem release -File | Where-Object Extension -in '.exe','.zip' | Get-FileHash -Algorithm SHA256 |
    ForEach-Object { "$($_.Hash.ToLower())  $([IO.Path]::GetFileName($_.Path))" } |
    Set-Content -Encoding ascii release\SHA256SUMS
} finally { Pop-Location }
Write-Output "Windows artifacts: $BuildRoot\release"
