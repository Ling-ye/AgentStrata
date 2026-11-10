# Synthetic native verification. Start-Process is intercepted; no application runs.
$ErrorActionPreference = 'Stop'
$Temporary = Join-Path ([IO.Path]::GetTempPath()) ('agentstrata-launch-test-'+[Guid]::NewGuid().ToString('N'))
$OldLocal = $env:LOCALAPPDATA
$LaunchLog = @{Count=0}
function Start-Process {
  param([string]$FilePath,[string]$WorkingDirectory)
  if ($FilePath -ne (Join-Path $Temporary 'program\app-test.exe') -or $WorkingDirectory -ne (Join-Path $Temporary 'program')) {throw 'Unexpected native launch command'}
  $LaunchLog.Count++
}
function Invoke-Check {
  & (Join-Path $PSScriptRoot 'launch.ps1') -Mode verify -RequestFile (Join-Path $Temporary 'request.json') -Executable (Join-Path $Temporary 'program\app-test.exe')
}
function Reject-Check {
  $Before = $LaunchLog.Count
  $Rejected = $false
  try {Invoke-Check | Out-Null} catch {$Rejected=$true}
  if (!$Rejected -or $LaunchLog.Count -ne $Before) {throw 'Unsafe fixture was not rejected before launch'}
}
try {
  New-Item -ItemType Directory -Path $Temporary | Out-Null
  # Fixture-only private ACL, never change permissions on an actual installation.
  $Acl = [Security.AccessControl.DirectorySecurity]::new()
  $User = [Security.Principal.WindowsIdentity]::GetCurrent().User
  $Acl.SetOwner($User)
  $Acl.SetAccessRuleProtection($true,$false)
  $Acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($User,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
  Set-Acl -LiteralPath $Temporary -AclObject $Acl
  $env:LOCALAPPDATA = Join-Path $Temporary 'local'
  $Program = Join-Path $Temporary 'program'
  $Root = Join-Path $env:LOCALAPPDATA 'AgentStrata\app-test\application'
  New-Item -ItemType Directory -Path (Join-Path $Program 'resources'),$Root -Force | Out-Null
  [IO.File]::WriteAllText((Join-Path $Program 'app-test.exe'),'synthetic executable, never run')
  [IO.File]::WriteAllText((Join-Path $Program 'resources\app.asar'),'synthetic archive')
  [IO.File]::WriteAllText((Join-Path $Root 'source.py'),'synthetic source')
  $Installed=@{schema='agentstrata-application-installation/v1';bot_id='test';bot_relative='bots/test/bot.yaml';platform='windows-x86_64';delivered_digest=('a'*64);artifacts_digest=('b'*64)}
  $Installed | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root '.agentstrata-application.json')
  $Files=@{}
  foreach($Name in @('app-test.exe','resources/app.asar')) {$Files[$Name]=(Get-FileHash -LiteralPath (Join-Path $Program $Name)).Hash.ToLowerInvariant()}
  $Request=@{launcher=@{schema='agentstrata-desktop-launch/v1';install_id='app-test';executable='app-test.exe';files=$Files};bot_id='test';bot_relative='bots/test/bot.yaml';source_digest=('a'*64);artifacts_digest=('b'*64);source_manifest=@{'source.py'=@{sha256=(Get-FileHash (Join-Path $Root 'source.py')).Hash.ToLowerInvariant()}}}
  $Request | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Temporary 'request.json')
  Invoke-Check | Out-Null
  if($LaunchLog.Count -ne 0) {throw 'Validation must never launch a process'}
  $Blocked=$false
  try { & (Join-Path $PSScriptRoot 'launch.ps1') -Mode launch -RequestFile (Join-Path $Temporary 'request.json') -Executable (Join-Path $Temporary 'program\app-test.exe') | Out-Null } catch {$Blocked=$true}
  if(!$Blocked -or $LaunchLog.Count -ne 0) {throw 'Missing runtime trust did not block launch'}
  [IO.File]::WriteAllText((Join-Path $Program 'extra.dll'),'injected')
  Reject-Check
  Remove-Item -LiteralPath (Join-Path $Program 'extra.dll')
  [IO.File]::WriteAllText((Join-Path $Root 'injected.py'),'injected module')
  Reject-Check
  Remove-Item -LiteralPath (Join-Path $Root 'injected.py')
  [IO.File]::WriteAllText((Join-Path $Root 'source.py'),'changed source')
  Reject-Check
  [IO.File]::WriteAllText((Join-Path $Root 'source.py'),'synthetic source')
  [IO.File]::WriteAllText((Join-Path $Program 'resources\app.asar'),'changed archive')
  Reject-Check
  Write-Output 'Native desktop validation: valid binding, runtime launch refusal and extra-file/source/archive tampering passed; no application launched.'
} finally {
  $env:LOCALAPPDATA = $OldLocal
  $Resolved = [IO.Path]::GetFullPath($Temporary)
  $Base = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
  if (!$Resolved.StartsWith($Base,[StringComparison]::OrdinalIgnoreCase) -or !(Split-Path -Leaf $Resolved).StartsWith('agentstrata-launch-test-')) {throw 'Unsafe test cleanup path'}
  if(Test-Path -LiteralPath $Resolved) {Remove-Item -LiteralPath $Resolved -Recurse -Force}
}
