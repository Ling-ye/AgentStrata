param(
  [Parameter(Mandatory=$true)][string]$BundleRoot,
  [string]$InstallRoot = (Join-Path $env:USERPROFILE '.agentstrata\applications\live2d-desktop-native'),
  [switch]$AuthOnly
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($InstallRoot -notmatch '^[A-Za-z]:[\\/]') { throw 'Use an absolute native Windows installation directory.' }
$Source = Join-Path $BundleRoot 'source'
$Desktop = Join-Path $Source 'integrations\live2d_desktop'
$Bundle = Get-Content -LiteralPath (Join-Path $BundleRoot 'application.json') -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath (Join-Path $BundleRoot 'artifacts.json') -Algorithm SHA256).Hash.ToLower() -ne $Bundle.artifacts_digest) {
  throw 'Native artifact manifest differs from the source-build package.'
}
$ArtifactCatalog = Get-Content -LiteralPath (Join-Path $BundleRoot 'artifacts.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($ArtifactCatalog.source_digest -ne $Bundle.source_digest) { throw 'Native artifacts identify another source.' }
foreach ($Member in $ArtifactCatalog.files.PSObject.Properties) {
  $Path = [IO.Path]::GetFullPath((Join-Path $BundleRoot $Member.Name))
  $Prefix = [IO.Path]::GetFullPath($BundleRoot).TrimEnd('\') + '\'
  if (!$Path.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase) -or $Member.Name.Contains(':') -or
      (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower() -ne $Member.Value) {
    throw 'Native dependency artifact identity mismatch.'
  }
}
$Lock = Get-Content -LiteralPath (Join-Path $Desktop 'windows-runtime.lock.json') -Raw | ConvertFrom-Json
$McpLock = Get-Content -LiteralPath (Join-Path $Desktop 'windows-mcp.lock.json') -Raw | ConvertFrom-Json
$Bootstrap = Join-Path $env:LOCALAPPDATA ('AgentStrata\bootstrap\' + $Bundle.source_digest)
New-Item -ItemType Directory -Path $Bootstrap -Force | Out-Null
$Logs = Join-Path $Bootstrap 'logs'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:UV_CACHE_DIR = Join-Path $env:LOCALAPPDATA 'AgentStrata\cache\uv'
$env:UV_LINK_MODE = 'copy'
$env:UV_NO_CONFIG = 'true'
function Run-Native([string]$File, [string[]]$Arguments, [string]$Name) {
  $Quoted = @($Arguments | ForEach-Object { '"' + $_.Replace('"','\"').TrimEnd('\') + '"' })
  $Out = Join-Path $Logs ($Name + '.out.log'); $Err = Join-Path $Logs ($Name + '.err.log')
  $Process = Start-Process -FilePath $File -ArgumentList $Quoted -WorkingDirectory $Bootstrap `
    -Wait -PassThru -NoNewWindow -RedirectStandardOutput $Out -RedirectStandardError $Err
  if ($Process.ExitCode -ne 0) { throw "Native preparation failed ($Name, exit $($Process.ExitCode)). Inspect $Err" }
}
function Fetch-Verified($Pin, [string]$Path) {
  if ((Test-Path -LiteralPath $Path) -and (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower() -eq $Pin.sha256) { return }
  $Pending = $Path + '.partial'
  Invoke-WebRequest -Uri $Pin.url -OutFile $Pending -UseBasicParsing
  if ((Get-FileHash -LiteralPath $Pending -Algorithm SHA256).Hash.ToLower() -ne $Pin.sha256) {
    Remove-Item -LiteralPath $Pending -Force
    throw 'Native dependency checksum mismatch; the incomplete file was not installed.'
  }
  Move-Item -LiteralPath $Pending -Destination $Path -Force
}
function Prepare-Environment([string]$PythonVersion, [string]$Environment, [string]$Requirements, [string]$Name) {
  $Python = Join-Path $Environment 'Scripts\python.exe'
  if (!(Test-Path -LiteralPath $Python)) {
    Run-Native $Uv @('venv','--clear','--no-config','--python',$PythonVersion,'--managed-python',$Environment) ($Name + '-venv')
  }
  Run-Native $Uv @('pip','sync','--no-config','--python',$Python,'--require-hashes','--only-binary',':all:',
    '--find-links',(Join-Path $BundleRoot 'wheels'),$Requirements) ($Name + '-dependencies')
  return $Python
}
function Install-AuthHelper {
  $Destination = Join-Path $InstallRoot 'runtime\tools\codex'
  $Executable = Join-Path $Destination 'codex.exe'
  if ((Test-Path -LiteralPath $Executable) -and (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash.ToLower() -eq $Lock.codex.binary_sha256) { return }
  $Archive = Join-Path $Bootstrap 'codex.tgz'
  Fetch-Verified $Lock.codex $Archive
  $Pending = Join-Path $Bootstrap 'codex-extract'
  New-Item -ItemType Directory -Path $Pending -Force | Out-Null
  Run-Native (Join-Path $env:SystemRoot 'System32\tar.exe') @('-xzf',$Archive,'-C',$Pending,$Lock.codex.member) 'extract-auth'
  $Binary = Join-Path $Pending $Lock.codex.member
  if ((Get-FileHash -LiteralPath $Binary -Algorithm SHA256).Hash.ToLower() -ne $Lock.codex.binary_sha256) { throw 'Native authentication helper identity mismatch.' }
  New-Item -ItemType Directory -Path $Destination -Force | Out-Null
  Copy-Item -LiteralPath $Binary -Destination $Executable -Force
}
if ($AuthOnly) {
  $Record = Get-Content -LiteralPath (Join-Path $InstallRoot '.agentstrata-application.json') -Raw | ConvertFrom-Json
  if ($Record.delivered_digest -ne $Bundle.source_digest) { throw 'Authentication resources belong to another application version.' }
  Install-AuthHelper
  Write-Output 'Native authentication helper is ready.'
  exit 0
}
$UvArchive = Join-Path $Bootstrap 'uv.zip'
Fetch-Verified $McpLock.uv $UvArchive
$Tools = Join-Path $Bootstrap 'tools'
Expand-Archive -LiteralPath $UvArchive -DestinationPath $Tools -Force
$Uv = (Get-ChildItem -LiteralPath $Tools -Filter uv.exe -Recurse | Select-Object -First 1).FullName
if (!$Uv -or (Get-FileHash -LiteralPath $Uv -Algorithm SHA256).Hash.ToLower() -ne $McpLock.uv.binary_sha256) { throw 'Native uv identity mismatch.' }
$Requirements = Join-Path $Desktop 'windows-host-requirements.lock'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Bootstrap 'python'
$BootstrapPython = Prepare-Environment $Lock.host_python (Join-Path $Bootstrap 'venv') $Requirements 'bootstrap'
$env:PYTHONPATH = Join-Path $Source 'src'
Run-Native $BootstrapPython @('-m','chatcopilot','app','install',$BundleRoot,'--root',$InstallRoot,'--source-only') 'install-source'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallRoot 'runtime\python'
$Python = Prepare-Environment $Lock.host_python (Join-Path $InstallRoot '.venv') $Requirements 'application'
# The public module entry always imports the installed source copy, without Git or an editable checkout.
$Pth = Join-Path $InstallRoot '.venv\Lib\site-packages\agentstrata-source.pth'
[IO.File]::WriteAllText($Pth, (Join-Path $InstallRoot 'src') + "`n", [Text.UTF8Encoding]::new($false))
$Plugin = Join-Path $InstallRoot '.agentstrata-data\plugins\open-llm-vtuber'
$PluginRequirements = Join-Path $BundleRoot 'vtuber-requirements.lock'
$PluginPython = Prepare-Environment $Lock.vtuber_python (Join-Path $Plugin 'runtime\venv') $PluginRequirements 'vtuber'
Run-Native (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') @('-NoProfile','-NonInteractive','-File',
  (Join-Path $Desktop 'scripts\prepare-windows-mcp.ps1'),'-SourceRoot',$Desktop,'-InstallRoot',(Join-Path $InstallRoot 'windows-mcp')) 'windows-mcp'
$Archive = Join-Path $Bootstrap 'ffmpeg.zip'
Fetch-Verified $Lock.ffmpeg $Archive
$Extracted = Join-Path $Bootstrap 'ffmpeg-extract'
Expand-Archive -LiteralPath $Archive -DestinationPath $Extracted -Force
$Ffmpeg = Join-Path $Extracted $Lock.ffmpeg.directory
$FfmpegDestination = Join-Path $Plugin 'runtime\tools\ffmpeg'
New-Item -ItemType Directory -Path $FfmpegDestination -Force | Out-Null
Copy-Item (Join-Path $Ffmpeg '*') $FfmpegDestination -Recurse -Force
$env:PYTHONPATH = Join-Path $InstallRoot 'src'
Run-Native $Python @('-m','chatcopilot','app','setup','--root',$InstallRoot,'--artifacts',$BundleRoot) 'setup-application'
$Launcher = Join-Path $InstallRoot '.venv\Scripts\agentstrata.cmd'
[IO.File]::WriteAllText($Launcher, '@"%~dp0python.exe" -m chatcopilot %*' + "`r`n", [Text.UTF8Encoding]::new($false))
Write-Output 'Native runtime is prepared. Configure this application model connection, then start the pet.'
