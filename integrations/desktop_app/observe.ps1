param([string]$InstallId,[string]$SourceDigest)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
if ($InstallId -notmatch '^[a-z][a-z0-9-]{1,80}$' -or $SourceDigest -notmatch '^[a-f0-9]{64}$') {throw 'Invalid build identity'}
$Root=Join-Path $env:LOCALAPPDATA ("AgentStrata\"+$InstallId+"\application")
$Python=Join-Path $Root '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $Python)) {Write-Output 'false';exit}
$env:PYTHONUTF8='1'
$Temporary=Join-Path ([IO.Path]::GetTempPath()) ('agentstrata-observe-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Temporary | Out-Null
try {
  # The installed Python launcher may use the Windows GUI subsystem.
  $Arguments=@('-m','chatcopilot','app','status','--root',('"'+$Root+'"'))
  $Output=Join-Path $Temporary 'status.json'
  $Errors=Join-Path $Temporary 'status.err'
  $Process=Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $Root -NoNewWindow -Wait -PassThru -RedirectStandardOutput $Output -RedirectStandardError $Errors
  if($Process.ExitCode -ne 0){throw 'Could not verify the running desktop application'}
  $Info=Get-Content -LiteralPath $Output -Raw -Encoding utf8 | ConvertFrom-Json
} finally {Remove-Item -LiteralPath $Temporary -Recurse -Force}
$Loaded=$Info.ok -and $Info.source_digest -eq $SourceDigest -and $Info.services.gateway.loaded
Write-Output ($Loaded | ConvertTo-Json -Compress)
