$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$Selection = Join-Path $env:USERPROFILE '.agentstrata\desktop-native\setup\desktop.json'
$Root = Join-Path $env:USERPROFILE '.agentstrata\applications\live2d-desktop-native'
if (Test-Path -LiteralPath $Selection) {
  $Settings = Get-Content -Raw -Encoding utf8 -LiteralPath $Selection | ConvertFrom-Json
  if ($Settings.connection.root) {$Root = $Settings.connection.root}
}
$InstalledFile = Join-Path $Root '.agentstrata-application.json'
if (!(Test-Path -LiteralPath $InstalledFile)) {throw 'The desktop has no prepared application.'}
$Installed = Get-Content -Raw -Encoding utf8 -LiteralPath $InstalledFile | ConvertFrom-Json
$Program = 'AgentStrata Live2D Native.exe'
$Known = @((Join-Path $env:LOCALAPPDATA 'Programs\AgentStrata Live2D Native'),
           (Join-Path $env:USERPROFILE '.agentstrata\build\live2d-native\desktop\release\win-unpacked'))
$MatchedPayloads = @(foreach ($Directory in $Known) {
  $Candidate = Join-Path $Directory 'resources\runtime-payload'
  $Manifest = Join-Path $Candidate 'application.json'
  if (!(Test-Path -LiteralPath (Join-Path $Directory $Program) -PathType Leaf) -or !(Test-Path -LiteralPath $Manifest -PathType Leaf)) {continue}
  $Record = Get-Content -Raw -Encoding utf8 -LiteralPath $Manifest | ConvertFrom-Json
  if ($Record.bot_id -eq 'live2d-desktop' -and $Record.source_digest -eq $Installed.delivered_digest -and
      $Record.artifacts_digest -eq $Installed.artifacts_digest) {$Candidate}
})
if ($MatchedPayloads.Count -eq 1) {
  $Payload = $MatchedPayloads[0]
} else {
  Add-Type -AssemblyName System.Windows.Forms
  $Dialog = New-Object System.Windows.Forms.OpenFileDialog
  try {
    $Dialog.Title = 'Select the installed AgentStrata Live2D Native executable'
    $Dialog.Filter = 'Windows application (*.exe)|*.exe'
    if ($Dialog.ShowDialog() -ne 'OK') {Write-Output 'null'; exit}
    $Payload = Join-Path (Split-Path $Dialog.FileName) 'resources\runtime-payload'
    if (!(Test-Path -LiteralPath (Join-Path $Payload 'application.json'))) {throw 'Select the installed desktop application.'}
  } finally {$Dialog.Dispose()}
}
@{root=$Root;assets=$Payload} | ConvertTo-Json -Compress
