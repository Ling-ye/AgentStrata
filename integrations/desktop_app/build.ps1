param([Parameter(Mandatory=$true)][string]$Stage,
      [switch]$Studio,
      [string]$BuildRoot = (Join-Path $env:LOCALAPPDATA 'AgentStrata\Studio\builders'))
$ErrorActionPreference = 'Stop'
$Recipe = $PSScriptRoot
$Live = Join-Path (Split-Path $Recipe) 'live2d_desktop'
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
$BuildTemp = Join-Path $BuildRoot 'temp'
New-Item -ItemType Directory -Force $BuildTemp | Out-Null
$env:TEMP = $BuildTemp; $env:TMP = $BuildTemp
$env:ELECTRON_BUILDER_CACHE = Join-Path $BuildRoot 'electron-builder-cache'
$env:electron_config_cache = Join-Path $BuildRoot 'electron-cache'
$env:npm_config_cache = Join-Path $BuildRoot 'npm-cache'
$Key = (Get-FileHash (Join-Path $Live 'package-lock.json')).Hash
$Guard=New-Object Threading.Mutex($false,("Local\AgentStrataStudioBuild"+$Key))
try {
try { [void]$Guard.WaitOne() } catch [Threading.AbandonedMutexException] { }
$NodeCmd = Get-Command node.exe -ErrorAction SilentlyContinue
if ($NodeCmd) {
  $Version = (Get-Item -LiteralPath $NodeCmd.Source).VersionInfo
  if ($Version.ProductMajorPart -lt 22 -or ($Version.ProductMajorPart -eq 22 -and $Version.ProductMinorPart -lt 12)) { $NodeCmd = $null }
}
if ($NodeCmd) { $Node = $NodeCmd.Source } else {
  $Node = Join-Path $BuildRoot 'node-v24.20.0-win-x64\node.exe'
  if (!(Test-Path -LiteralPath $Node)) {
    $Archive = Join-Path $BuildRoot 'node.zip'
    Invoke-WebRequest -UseBasicParsing 'https://nodejs.org/dist/v24.20.0/node-v24.20.0-win-x64.zip' -OutFile $Archive
    if ((Get-FileHash $Archive).Hash.ToLower() -ne '6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba') {throw 'Invalid Windows Node archive'}
    Expand-Archive $Archive $BuildRoot -Force
  }
}
$Tools = Join-Path $BuildRoot $Key
if (!(Test-Path (Join-Path $Tools 'ready'))) {
  New-Item -ItemType Directory -Force $Tools | Out-Null
  Copy-Item (Join-Path $Live 'package*.json') $Tools -Force
  Push-Location $Tools
  try {
    $env:Path = (Split-Path $Node) + ';' + $env:Path
    $env:PATHEXT = '.COM;.EXE;.BAT;.CMD'
    $NpmArguments=@((Join-Path (Split-Path $Node) 'node_modules\npm\bin\npm-cli.js'),'ci','--no-audit','--no-fund') | ForEach-Object {'"'+$_+'"'}
    $Npm=Start-Process -FilePath $Node -ArgumentList $NpmArguments -WorkingDirectory $Tools -NoNewWindow -Wait -PassThru
    if ($Npm.ExitCode -ne 0) {throw 'Windows build tooling setup failed'}
    New-Item -ItemType File -Force (Join-Path $Tools 'ready') | Out-Null
  } finally {Pop-Location}
}
if (!(Test-Path -LiteralPath (Join-Path $Tools 'node_modules\electron\dist\electron.exe'))) {
  $InstallElectron = Join-Path $Tools 'node_modules\electron\install.js'
  $Process = Start-Process -FilePath $Node -ArgumentList ('"' + $InstallElectron + '"') -WorkingDirectory $Tools -NoNewWindow -Wait -PassThru
  if ($Process.ExitCode -ne 0) {throw 'Pinned Electron runtime download failed'}
}
} finally { $Guard.ReleaseMutex(); $Guard.Dispose() }
$Job = Join-Path $BuildRoot ([Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $Job | Out-Null
try {
  if ($Studio) {
    Copy-Item -LiteralPath (Join-Path $Stage 'framework.tar.gz') -Destination $Job
    Copy-Item -LiteralPath (Join-Path $Stage 'framework.sha256') -Destination $Job
  } else {
    # Managed project paths can exceed the legacy PowerShell file-provider limit.
    $CopyArguments=@(('"'+(Join-Path $Stage 'runtime-payload')+'"'),('"'+(Join-Path $Job 'runtime-payload')+'"'),'/E','/COPY:DAT','/DCOPY:DA','/R:0','/W:0','/NFL','/NDL','/NJH','/NJS','/NP')
    $Copy=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\robocopy.exe') -ArgumentList $CopyArguments -NoNewWindow -Wait -PassThru
    if ($Copy.ExitCode -ge 8) {throw 'Windows runtime payload copy failed.'}
  }
  Copy-Item -LiteralPath (Join-Path $Stage 'desktop-build.json') -Destination $Job
  Push-Location $Job
  try {
    $Arguments = @((Join-Path $Recipe 'package.cjs'), $Tools, $Job, $Recipe) | ForEach-Object { '"' + $_ + '"' }
    $env:PATHEXT = '.COM;.EXE;.BAT;.CMD'
    $env:Path = (Split-Path $Node) + ';' + $env:Path
    $Out = Join-Path $Job 'pack.out.log'
    $Err = Join-Path $Job 'pack.err.log'
    $Process = Start-Process -FilePath $Node -ArgumentList $Arguments -WorkingDirectory $Job -NoNewWindow -Wait -PassThru -RedirectStandardOutput $Out -RedirectStandardError $Err
    if ($Process.ExitCode -ne 0) {throw ('Windows packaging failed: ' + (Get-Content -LiteralPath $Err -Raw))}
    Copy-Item -LiteralPath (Join-Path $Job 'release\application.exe') -Destination $Stage
    if (!$Studio) {Copy-Item -LiteralPath (Join-Path $Job 'desktop-launch.json') -Destination $Stage}
  } finally {Pop-Location}
} finally {
  # Job is a fresh GUID directory owned by this build; the reusable tooling stays.
  Set-Location -LiteralPath $BuildRoot
  [Environment]::CurrentDirectory = $BuildRoot
  if (Test-Path -LiteralPath $Job) {
    try { Remove-Item -LiteralPath $Job -Recurse -Force } catch { Write-Warning "Temporary build directory remains: $Job" }
  }
}
