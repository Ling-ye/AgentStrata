param([string]$Distro,[string]$User,[string]$Framework,[string]$Python,[string]$StudioHome,[ValidateSet('main','worker')][string]$Lane,[string]$AuthRoot="",[string]$CodexBin="")
$ErrorActionPreference = 'Stop'
$Values = @('-d',$Distro,'-u',$User,'--cd',$Framework,'--',$Python,'-m','studio','login','--home',$StudioHome,'--lane',$Lane,'--auth-root',$AuthRoot,'--codex-bin',$CodexBin)
if ($Values.Where({$_ -match '["\r\n]'}).Count) {throw 'Invalid local login path'}
$Arguments='-d '+$Distro+' -u '+$User+' --exec /usr/bin/env -C "'+$Framework+'" "'+$Python+'" -m studio login --home "'+$StudioHome+'" --lane '+$Lane
if ($AuthRoot) {$Arguments += ' --auth-root "'+$AuthRoot+'"'}
if ($CodexBin) {$Arguments += ' --codex-bin "'+$CodexBin+'"'}
Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\wsl.exe') -ArgumentList $Arguments
