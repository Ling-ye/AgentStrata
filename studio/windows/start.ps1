param([string]$Launcher)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$Wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$Distro = 'Ubuntu-24.04'
function New-StudioWslProcessInfo([string[]]$Values) {
  if ($Values.Where({$_ -match '["\r\n\x00]'}).Count) {throw 'Invalid local WSL argument'}
  $Info=New-Object System.Diagnostics.ProcessStartInfo
  $Info.FileName=$Wsl; $Info.UseShellExecute=$false; $Info.CreateNoWindow=$true
  $Info.RedirectStandardOutput=$true; $Info.RedirectStandardError=$true
  $Info.StandardOutputEncoding=[System.Text.Encoding]::UTF8
  $Info.StandardErrorEncoding=[System.Text.Encoding]::UTF8
  $Info.EnvironmentVariables['WSL_UTF8']='1'
  # WSL needs its option names unquoted. Quote empty/whitespace arguments and
  # double trailing backslashes before a closing Windows argument quote.
  # --exec at each call keeps Linux shell expansion out of installation paths.
  $Info.Arguments=(@($Values | ForEach-Object {
    $Value=[string]$_
    if (!$Value.Length -or $Value -match '\s') {'"'+($Value -replace '(\\+)$','$1$1')+'"'} else {$Value}
  })) -join ' '
  return $Info
}
function Invoke-StudioWsl([string[]]$Values,[string]$FailureMessage='WSL command failed',
                         [int[]]$SuccessExitCodes=@(0),[switch]$PassThru) {
  $Process=[System.Diagnostics.Process]::Start((New-StudioWslProcessInfo -Values $Values))
  try {
    # PowerShell 5.1 treats native stderr as an error with Stop, even when WSL
    # succeeds. Drain both pipes independently and use the actual exit code.
    $OutputTask=$Process.StandardOutput.ReadToEndAsync()
    $ErrorTask=$Process.StandardError.ReadToEndAsync()
    $Process.WaitForExit()
    $Output=$OutputTask.GetAwaiter().GetResult()
    $Details=$ErrorTask.GetAwaiter().GetResult()
    if ($Process.ExitCode -notin $SuccessExitCodes) {
      if (!$Details) {$Details=$Output}
      if ($Details.Length -gt 1600) {$Details=$Details.Substring($Details.Length-1600)}
      throw ($FailureMessage+' (exit '+$Process.ExitCode+'). '+$Details)
    }
    if ($PassThru) {return [pscustomobject]@{Output=$Output;ExitCode=$Process.ExitCode}}
    return $Output
  } finally {$Process.Dispose()}
}
function Initialize-StudioDistro([string]$ResumeLauncher) {
  $Listing=Invoke-StudioWsl -Values @('--list','--quiet') -FailureMessage 'Could not read WSL distributions for the current Windows user. Prepare WSL system components first, then retry from this account.'
  $Distros=@($Listing -split "`n" | ForEach-Object {($_ -replace "`0",'').Trim()})
  if ($Distros -notcontains $Distro) {
    Progress '正在当前 Windows 账号下准备 Ubuntu。WSL 系统组件须先完成启用。'
    if ($ResumeLauncher) {New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'AgentStrataStudioSetup' -Value ('"' + $ResumeLauncher + '"') -PropertyType String -Force | Out-Null}
    # WSL distributions belong to the Windows user. RunAs can register Ubuntu
    # under a different administrator, so keep this process and identity.
    $Setup=Invoke-StudioWsl -Values @('--install','--no-launch','-d',$Distro) -SuccessExitCodes @(0,3010) -PassThru -FailureMessage 'Ubuntu setup failed for the current Windows user. If system components require administrator preparation, finish that separately and retry from this same account.'
    if ($Setup.ExitCode -eq 3010) {Progress '请重启 Windows，并回到同一账号继续准备工作台。';return $false}
    $Listing=Invoke-StudioWsl -Values @('--list','--quiet') -FailureMessage 'Could not read installed WSL distributions'
    $Distros=@($Listing -split "`n" | ForEach-Object {($_ -replace "`0",'').Trim()})
    if ($Distros -notcontains $Distro) {Progress '请重启 Windows，并回到同一账号继续准备工作台。';return $false}
  }
  return $true
}
$Form = New-Object System.Windows.Forms.Form
$Form.Text='AgentStrata Studio'; $Form.Width=530; $Form.Height=265; $Form.StartPosition='CenterScreen'
$Label=New-Object System.Windows.Forms.Label; $Label.SetBounds(24,22,462,98)
$Label.Text='首次使用会在本机准备 WSL2、Python 和开发工具，之后会打开浏览器。请保持联网。'
$Button=New-Object System.Windows.Forms.Button; $Button.SetBounds(24,135,450,38); $Button.Text='准备并打开开发工作台'
$Form.Controls.AddRange(@($Label,$Button))
function Progress([string]$Text){$Label.Text=$Text;$Form.Refresh()}
$Button.Add_Click({
  $Button.Enabled=$false
  try {
    if (!(Initialize-StudioDistro -ResumeLauncher $Launcher)) {return}
    Progress '正在准备本机开发环境，首次下载可能需要几分钟。'
    $LinuxResources=(Invoke-StudioWsl -Values @('-d',$Distro,'-u','root','--exec','wslpath','-u',$PSScriptRoot) -FailureMessage 'Could not open the WSL distribution').Trim()
    Invoke-StudioWsl -Values @('-d',$Distro,'-u','root','--exec','bash',"$LinuxResources/bootstrap.sh",'system') -FailureMessage 'WSL system setup failed. Check WSL systemd and network access, then retry.' | Out-Null
    $Values=@('-d',$Distro,'-u','agentstrata-studio','--exec','bash',"$LinuxResources/bootstrap.sh",'launch',$LinuxResources)
    $Info=New-StudioWslProcessInfo -Values $Values
    $script:Session=[System.Diagnostics.Process]::Start($Info)
    $Errors=$script:Session.StandardError.ReadToEndAsync()
    $Url=''
    while ($null -ne ($Line=$script:Session.StandardOutput.ReadLine())) {
      if($Line -match '^http://127\.0\.0\.1:[0-9]+/#login=[A-Za-z0-9_-]+$') {$Url=$Line;break}
    }
    if ($Url -notmatch '^http://127\.0\.0\.1:[0-9]+/#login=[A-Za-z0-9_-]+$') {
      $Failure=$Errors.GetAwaiter().GetResult()
      if ($Failure.Length -gt 1600) {$Failure=$Failure.Substring($Failure.Length-1600)}
      throw ('Local web service did not become ready. '+$Failure)
    }
    Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'AgentStrataStudioSetup' -ErrorAction SilentlyContinue
    Start-Process $Url; $Form.Close()
  } catch {Progress $_.Exception.Message} finally {$Button.Enabled=$true}
})
[void]$Form.ShowDialog()
if ($script:Session -and !$script:Session.HasExited) { $script:Session.WaitForExit() }
