param([string]$Launcher)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$Wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$Distro = 'Ubuntu-24.04'
$Form = New-Object System.Windows.Forms.Form
$Form.Text='AgentStrata 开发工作台'; $Form.Width=530; $Form.Height=265; $Form.StartPosition='CenterScreen'
$Label=New-Object System.Windows.Forms.Label; $Label.SetBounds(24,22,462,98)
$Label.Text='首次使用会在本机准备 WSL2、Python 和开发工具，之后会打开浏览器。请保持联网。'
$Button=New-Object System.Windows.Forms.Button; $Button.SetBounds(24,135,450,38); $Button.Text='准备并打开开发工作台'
$Form.Controls.AddRange(@($Label,$Button))
function Progress([string]$Text){$Label.Text=$Text;$Form.Refresh()}
$Button.Add_Click({
  $Button.Enabled=$false
  try {
    $Distros=@(& $Wsl --list --quiet 2>$null | ForEach-Object {($_ -replace "`0",'').Trim()})
    if ($Distros -notcontains $Distro) {
      Progress '正在启用 WSL2 和 Ubuntu，Windows 可能会请求管理员授权。'
      if ($Launcher) {New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'AgentStrataStudioSetup' -Value ('"' + $Launcher + '"') -PropertyType String -Force | Out-Null}
      $Setup=Start-Process -FilePath $Wsl -ArgumentList @('--install','--no-launch','-d',$Distro) -Verb RunAs -Wait -PassThru
      if ($Setup.ExitCode -notin @(0,3010)) {throw 'Windows could not install WSL2. Check virtualization and run setup again.'}
      $Distros=@(& $Wsl --list --quiet | ForEach-Object {($_ -replace "`0",'').Trim()})
      if ($Setup.ExitCode -eq 3010 -or $Distros -notcontains $Distro) {Progress '请重启 Windows，登录后会继续准备工作台。';return}
    }
    Progress '正在准备本机开发环境，首次下载可能需要几分钟。'
    $LinuxResources=(& $Wsl -d $Distro -u root -- wslpath -u $PSScriptRoot).Trim()
    if ($LASTEXITCODE -ne 0) {throw 'Could not open the WSL distribution'}
    & $Wsl -d $Distro -u root -- bash "$LinuxResources/bootstrap.sh" system
    if ($LASTEXITCODE -ne 0) {throw 'WSL system setup failed. Check WSL systemd and network access, then retry.'}
    $Info=New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName=$Wsl; $Info.UseShellExecute=$false; $Info.CreateNoWindow=$true; $Info.RedirectStandardOutput=$true
    $Values=@('-d',$Distro,'-u','agentstrata-studio','--','bash',"$LinuxResources/bootstrap.sh",'launch',$LinuxResources)
    if ($Values.Where({$_ -match '["\r\n]'}).Count) {throw 'Invalid local installation path'}
    $Info.Arguments='-d '+$Distro+' -u agentstrata-studio --exec bash "'+$LinuxResources+'/bootstrap.sh" launch "'+$LinuxResources+'"'
    $script:Session=[System.Diagnostics.Process]::Start($Info)
    $Url=''
    while ($null -ne ($Line=$script:Session.StandardOutput.ReadLine())) {
      if($Line -match '^http://127\.0\.0\.1:[0-9]+/#login=[A-Za-z0-9_-]+$') {$Url=$Line;break}
    }
    if ($Url -notmatch '^http://127\.0\.0\.1:[0-9]+/#login=[A-Za-z0-9_-]+$') {throw 'Local web service did not become ready'}
    Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'AgentStrataStudioSetup' -ErrorAction SilentlyContinue
    Start-Process $Url; $Form.Close()
  } catch {Progress $_.Exception.Message} finally {$Button.Enabled=$true}
})
[void]$Form.ShowDialog()
if ($script:Session -and !$script:Session.HasExited) { $script:Session.WaitForExit() }
