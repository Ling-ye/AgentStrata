param(
  [string]$SourceRoot = (Split-Path $PSScriptRoot -Parent),
  [string]$InstallRoot = (Join-Path $env:USERPROFILE '.agentstrata\applications\live2d-desktop\windows-mcp')
)
$ErrorActionPreference = 'Stop'
if ($InstallRoot.StartsWith('\\')) { throw 'InstallRoot must be a native Windows directory.' }
$Lock = Get-Content -LiteralPath (Join-Path $SourceRoot 'windows-mcp.lock.json') -Raw | ConvertFrom-Json
$Receipt = Join-Path $InstallRoot 'installation.json'
$Python = Join-Path $InstallRoot 'venv\Scripts\python.exe'
if (Test-Path -LiteralPath $Receipt) {
  $Installed = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
  if ($Installed.revision -ne $Lock.revision -or $Installed.python_version -ne $Lock.python -or $Installed.source_sha256 -ne $Lock.archive_sha256) {
    throw 'Another Windows-MCP environment owns this directory. Select a new directory.'
  }
  if (!(Test-Path -LiteralPath $Python)) { throw 'The managed Windows-MCP Python is missing.' }
  $Installed | ConvertTo-Json -Compress
  exit 0
}
$Preparing = Join-Path $InstallRoot 'preparing.json'
if ((Test-Path -LiteralPath $InstallRoot) -and @(Get-ChildItem -LiteralPath $InstallRoot -Force).Count -gt 0 -and !(Test-Path -LiteralPath $Preparing)) {
  throw 'The destination is not an empty or managed preparation directory.'
}
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
if (Test-Path -LiteralPath $Preparing) {
  if ((Get-Content -LiteralPath $Preparing -Raw | ConvertFrom-Json).revision -ne $Lock.revision) { throw 'Another source owns this preparation.' }
} else { @{revision=$Lock.revision} | ConvertTo-Json | Set-Content -LiteralPath $Preparing -Encoding utf8 }
$Logs = Join-Path $InstallRoot 'logs'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null
function Fetch-Verified([string]$Url, [string]$Hash, [string]$Path) {
  if (!(Test-Path -LiteralPath $Path)) { Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing }
  if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower() -ne $Hash) {
    throw 'Windows-MCP dependency archive checksum mismatch.'
  }
}
function Run-Native([string]$File, [string[]]$Arguments, [string]$Name) {
  $Out = Join-Path $Logs ($Name + '.out.log'); $Err = Join-Path $Logs ($Name + '.err.log')
  $Quoted = @($Arguments | ForEach-Object { '"' + $_.Replace('"','\"').TrimEnd('\') + '"' })
  $Process = Start-Process -FilePath $File -ArgumentList $Quoted -WorkingDirectory $InstallRoot `
    -Wait -PassThru -NoNewWindow -RedirectStandardOutput $Out -RedirectStandardError $Err
  if ($Process.ExitCode -ne 0) { throw "Windows-MCP $Name failed with exit $($Process.ExitCode); inspect $Err" }
  if (Test-Path -LiteralPath $Out) { Get-Content -LiteralPath $Out -Raw }
}
$UvArchive = Join-Path $InstallRoot 'uv.zip'
Fetch-Verified $Lock.uv.url $Lock.uv.sha256 $UvArchive
$Tools = Join-Path $InstallRoot 'tools'
Expand-Archive -LiteralPath $UvArchive -DestinationPath $Tools -Force
$Uv = (Get-ChildItem -LiteralPath $Tools -Filter uv.exe -Recurse | Select-Object -First 1).FullName
if (!$Uv -or (Get-FileHash -LiteralPath $Uv -Algorithm SHA256).Hash.ToLower() -ne $Lock.uv.binary_sha256) {
  throw 'Native uv does not match its pinned binary.'
}
$Archive = Join-Path $InstallRoot 'windows-mcp.tar.gz'
Fetch-Verified $Lock.archive_url $Lock.archive_sha256 $Archive
$Source = Join-Path $InstallRoot 'source'
New-Item -ItemType Directory -Path $Source -Force | Out-Null
Run-Native (Join-Path $env:SystemRoot 'System32\tar.exe') @('-xzf',$Archive,'--strip-components=1','-C',$Source) 'extract' | Out-Null
$env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallRoot 'python'
$env:UV_CACHE_DIR = Join-Path $InstallRoot 'cache'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $InstallRoot 'venv'
$env:ANONYMIZED_TELEMETRY = 'false'
$env:PYTHONUTF8 = '1'
Run-Native $Uv @('sync','--frozen','--no-dev','--no-config','--project',$Source,'--python',$Lock.python,'--managed-python') 'sync' | Out-Null
$Verify = Join-Path $InstallRoot 'verify.py'
@'
import importlib.metadata, json, platform, sys
print(json.dumps({'package_version': importlib.metadata.version('windows-mcp'), 'python_version': platform.python_version(), 'python': sys.executable}))
'@ | Set-Content -LiteralPath $Verify -Encoding utf8
$Identity = Run-Native $Python @($Verify) 'verify' | ConvertFrom-Json
if ($Identity.package_version -ne $Lock.package_version -or $Identity.python_version -ne $Lock.python) {
  throw 'Prepared Windows-MCP runtime identity differs from the lock.'
}
$Result = [ordered]@{revision=$Lock.revision; source_sha256=$Lock.archive_sha256; package_version=$Identity.package_version;
  python_version=$Identity.python_version; python=$Identity.python; root=$InstallRoot}
$Result | ConvertTo-Json | Set-Content -LiteralPath $Receipt -Encoding utf8
$Result | ConvertTo-Json -Compress
