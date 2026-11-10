param(
  [Parameter(Mandatory=$true)][ValidateSet('select','verify','launch')][string]$Mode,
  [Parameter(Mandatory=$true)][string]$RequestFile,
  [string]$Executable
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($Mode -eq 'launch') {throw 'Desktop launch unavailable: prepared runtime dependencies have no independently trusted integrity evidence'}
$Request = Get-Content -LiteralPath $RequestFile -Raw -Encoding utf8 | ConvertFrom-Json
$Manifest = $Request.launcher
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$Allowed = @($Sid, 'S-1-5-18', 'S-1-5-32-544')
$Handles = [Collections.Generic.List[IDisposable]]::new()

function Assert-Path([string]$Path) {
  $Item = Get-Item -LiteralPath $Path -Force
  if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Reparse points are not supported'}
  $Parent = if ($Item.PSIsContainer) {$Item.Parent} else {$Item.Directory}
  while ($Parent) {
    if ($Parent.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Reparse parent is not supported'}
    $Parent = $Parent.Parent
  }
  $Acl = Get-Acl -LiteralPath $Path
  if ($Acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $Sid) {throw 'Installation must belong to this user'}
  $Write = [Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
  foreach ($Rule in $Acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
    if ($Rule.AccessControlType -eq 'Allow' -and ($Rule.FileSystemRights -band $Write) -and $Rule.IdentityReference.Value -notin $Allowed) {
      throw 'Installation is writable by another principal'
    }
  }
}
function Hold-Hash([string]$Path, [string]$Expected) {
  Assert-Path $Path
  if ($Expected -cnotmatch '^[a-f0-9]{64}$') {throw 'Invalid expected file digest'}
  # Prevent replacement/write between verification and process creation.
  $Stream = [IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
  $Handles.Add($Stream)
  $Hasher = [Security.Cryptography.SHA256]::Create()
  try {$Actual = ([BitConverter]::ToString($Hasher.ComputeHash($Stream))).Replace('-','').ToLowerInvariant()}
  finally {$Hasher.Dispose()}
  if ($Actual -cne $Expected) {throw 'Installed file differs from the selected build'}
}
function Member([string]$Root,[string]$Relative) {
  if (!$Relative -or $Relative -match '(^/|\\|:|(^|/)\.\.?(/|$)|[\x00-\x1f])') {throw 'Invalid launch manifest member'}
  return Join-Path $Root $Relative.Replace('/','\')
}
try {
  if ($Manifest.schema -cne 'agentstrata-desktop-launch/v1' -or
      $Manifest.install_id -cnotmatch '^[a-z][a-z0-9-]{1,80}$' -or
      $Manifest.executable -cne ($Manifest.install_id+'.exe')) {throw 'Invalid launch identity'}
  if ($Mode -eq 'select') {
    Add-Type -AssemblyName System.Windows.Forms
    $Dialog = New-Object System.Windows.Forms.OpenFileDialog
    try {
      $Dialog.Title = 'Select the installed executable for this exact build'
      $Dialog.Filter = 'Windows application (*.exe)|*.exe'
      if ($Dialog.ShowDialog() -ne 'OK') {throw 'Selection cancelled'}
      $Executable = $Dialog.FileName
    } finally {$Dialog.Dispose()}
  }
  if (!$Executable -or $Executable -match '["\x00-\x1f]' -or $Executable.StartsWith('\\') -or
      [IO.Path]::GetFullPath($Executable) -cne $Executable -or
      [IO.Path]::GetFileName($Executable) -cne $Manifest.executable) {throw 'Invalid installed executable'}
  $Directory = Split-Path -Parent $Executable
  Assert-Path $Directory
  $Names = @($Manifest.files.PSObject.Properties.Name)
  if ($Names.Count -lt 2 -or $Manifest.executable -cnotin $Names -or 'resources/app.asar' -cnotin $Names) {throw 'Incomplete launcher manifest'}
  foreach ($Entry in $Manifest.files.PSObject.Properties) {Hold-Hash (Member $Directory $Entry.Name) $Entry.Value}
  # NSIS writes one uninstaller. No extra DLL, executable, ASAR or resource is trusted.
  foreach ($File in Get-ChildItem -LiteralPath $Directory -Recurse -Force) {
    Assert-Path $File.FullName
    if ($File.PSIsContainer) {continue}
    $Name = $File.FullName.Substring($Directory.Length+1).Replace('\','/')
    if ($Name -cnotin $Names -and $Name -notmatch '^Uninstall [^/]+\.exe$') {throw 'Unregistered file in installation'}
  }
  $Root = Join-Path $env:LOCALAPPDATA ('AgentStrata\'+$Manifest.install_id+'\application')
  Assert-Path $Root
  if (Test-Path -LiteralPath (Join-Path $Root '.agentstrata-versions\current.json')) {throw 'Selected runtime version changed; bind a matching build'}
  $InstalledFile = Join-Path $Root '.agentstrata-application.json'
  Assert-Path $InstalledFile
  $Installed = Get-Content -LiteralPath $InstalledFile -Raw -Encoding utf8 | ConvertFrom-Json
  if ($Installed.schema -cne 'agentstrata-application-installation/v1' -or
      $Installed.bot_id -cne $Request.bot_id -or $Installed.bot_relative -cne $Request.bot_relative -or $Installed.platform -cne 'windows-x86_64' -or
      $Installed.delivered_digest -cne $Request.source_digest -or
      $Installed.artifacts_digest -cne $Request.artifacts_digest) {throw 'Runtime installation does not match this build'}
  foreach ($Entry in $Request.source_manifest.PSObject.Properties) {
    Hold-Hash (Member $Root $Entry.Name) $Entry.Value.sha256
  }
  # Match the canonical source inventory boundary; private data/dependencies are separate.
  $SourceNames = @($Request.source_manifest.PSObject.Properties.Name)
  function Check-SourceTree([string]$Base,[string]$Relative='') {
    foreach ($Item in Get-ChildItem -LiteralPath $Base -Force) {
      $Name = if ($Relative) {$Relative+'/'+$Item.Name} else {$Item.Name}
      if ($Item.PSIsContainer) {
        if ($Item.Name -cin @($Request.ignored_parts) -or (!$Relative -and ($Item.Name -cin @($Request.ignored_top) -or $Item.Name -cin @('runtime','windows-mcp')))) {continue}
        Assert-Path $Item.FullName
        Check-SourceTree $Item.FullName $Name
      } else {
        if (!$Relative -and $Item.Name -cin @('.agentstrata-application.json','.agentstrata-source.json')) {continue}
        # Only private environment files are permitted outside the frozen inventory.
        if ($Item.Name -eq '.env' -or $Item.Name.StartsWith('.env.') -or $Item.Name.EndsWith('.env')) {continue}
        if ($Name -cnotin $SourceNames) {throw 'Unregistered source file in runtime installation'}
      }
    }
  }
  Check-SourceTree $Root
  @{executable=$Executable; state='bound'; runtime_verified=$false} | ConvertTo-Json -Compress
} finally {
  foreach ($Handle in $Handles) {$Handle.Dispose()}
}
