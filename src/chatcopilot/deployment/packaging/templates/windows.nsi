Unicode true
Name "AgentStrata 应用安装"
OutFile "application.exe"
RequestExecutionLevel user
SetCompress off
CRCCheck force
ShowInstDetails show
Page instfiles
Section
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File "payload.tgz"
  File "install.ps1"
  File "install-receipt.ps1"
  StrCpy $1 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
  IfFileExists "$1" +2 0
    StrCpy $1 "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"
  StrCpy $2 ""
  IfSilent 0 +2
    StrCpy $2 "-Quiet"
  ExecWait '"$1" -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\install.ps1" -Bot "@BOT@" -Hash "@SHA@" -Installer "$EXEPATH" @DOCKER@ $2' $0
  IfErrors failure
  StrCmp $0 0 done
  failure:
    SetErrorLevel 1
    MessageBox MB_ICONSTOP|MB_OK "安装未完成。请核对安装环境和前一步提示，再运行此安装包。" /SD IDOK
    Abort
  done:
SectionEnd
