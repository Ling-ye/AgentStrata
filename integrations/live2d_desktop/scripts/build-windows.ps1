param(
  [string]$SourceRoot = (Split-Path $PSScriptRoot -Parent),
  [string]$BuildRoot = (Join-Path $env:USERPROFILE '.agentstrata\build\live2d-native\desktop'),
  [string]$RuntimePayload,
  [switch]$VerifyOnly
)
$ErrorActionPreference = 'Stop'
if ($BuildRoot.StartsWith('\\')) { throw 'BuildRoot must be on a native Windows drive, not UNC.' }
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
# Explicit public inputs. Do not copy a checkout cache, userData, WSL config or character assets.
foreach ($name in @('package.json','package-lock.json','tsconfig.json','windows-mcp.lock.json','windows-runtime.lock.json')) {
  Copy-Item (Join-Path $SourceRoot $name) (Join-Path $BuildRoot $name) -Force
}
foreach ($name in @('src','ui','scripts','test','licenses')) {
  New-Item -ItemType Directory -Path (Join-Path $BuildRoot $name) -Force | Out-Null
  Copy-Item (Join-Path $SourceRoot "$name\*") (Join-Path $BuildRoot $name) -Recurse -Force
}
$RepoRoot = Split-Path (Split-Path $SourceRoot -Parent) -Parent
if (!$VerifyOnly) {
  if (!$RuntimePayload) { throw 'Native packaging requires a prepared RuntimePayload; verification alone does not build one.' }
  $PayloadRecord = Get-Content -LiteralPath (Join-Path $RuntimePayload 'application.json') -Raw | ConvertFrom-Json
  $DesktopInputs = @(Get-ChildItem -LiteralPath (Join-Path $SourceRoot 'src') -Recurse -File) +
    @(Get-ChildItem -LiteralPath (Join-Path $SourceRoot 'ui') -Recurse -File | Where-Object Extension -ne '.ico') +
    @('package.json','package-lock.json','scripts\resources.cjs' | ForEach-Object { Get-Item -LiteralPath (Join-Path $SourceRoot $_) })
  foreach ($File in $DesktopInputs) {
    $Relative = $File.FullName.Substring($SourceRoot.TrimEnd('\').Length + 1)
    $Expected = Join-Path $RuntimePayload ('source\integrations\live2d_desktop\' + $Relative)
    if (!(Test-Path $Expected) -or (Get-FileHash $File.FullName).Hash -ne (Get-FileHash $Expected).Hash) {
      throw 'Desktop source and native runtime payload differ. Rebuild the payload first.'
    }
  }
  $Destination = Join-Path $BuildRoot 'runtime-payload'
  if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
  Copy-Item -LiteralPath $RuntimePayload -Destination $Destination -Recurse
}
foreach ($name in @('LICENSE','COPYRIGHT')) {
  Copy-Item (Join-Path $RepoRoot $name) (Join-Path $BuildRoot "licenses\$name") -Force
}
foreach ($name in @('Open-LLM-VTuber-LICENSE.txt','Open-LLM-VTuber-Web-LICENSE.txt')) {
  Copy-Item (Join-Path $RepoRoot "third_party\$name") (Join-Path $BuildRoot "licenses\$name") -Force
}
$Inputs = [ordered]@{}
foreach ($name in @('src','ui','scripts','test','licenses')) {
  Get-ChildItem -LiteralPath (Join-Path $BuildRoot $name) -Recurse -File | ForEach-Object {
    $Relative = $_.FullName.Substring($BuildRoot.TrimEnd('\').Length + 1).Replace('\','/')
    $Inputs[$Relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower()
  }
}
foreach ($name in @('package.json','package-lock.json','tsconfig.json','windows-mcp.lock.json','windows-runtime.lock.json')) {
  $Inputs[$name] = (Get-FileHash -LiteralPath (Join-Path $BuildRoot $name) -Algorithm SHA256).Hash.ToLower()
}
Push-Location $BuildRoot
try {
  $Node = (Get-Command node.exe -ErrorAction Stop).Source
  $Runner = Join-Path $BuildRoot 'scripts\build-native.cjs'
  $NodeArguments = @('"' + $Runner + '"')
  if ($VerifyOnly) { $NodeArguments += '--verify-only' }
  $process = Start-Process -FilePath $Node -ArgumentList $NodeArguments -WorkingDirectory $BuildRoot -NoNewWindow -Wait -PassThru
  if ($process.ExitCode -ne 0) { throw "Desktop build failed: $($process.ExitCode)" }
  if (!$VerifyOnly) {
    $Inputs | ConvertTo-Json | Set-Content -Encoding utf8 release\source-inputs.json
    Get-ChildItem release -File | Where-Object Extension -in '.exe','.zip' | Get-FileHash -Algorithm SHA256 |
      ForEach-Object { "$($_.Hash.ToLower())  $([IO.Path]::GetFileName($_.Path))" } |
      Set-Content -Encoding ascii release\SHA256SUMS
  }
} finally { Pop-Location }
if ($VerifyOnly) { Write-Output 'Windows verification complete; installer packaging was not requested.' }
else { Write-Output "Windows artifacts: $BuildRoot\release" }
