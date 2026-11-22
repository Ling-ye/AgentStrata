Unicode true
Name "@NAME@"
OutFile "release/application.exe"
InstallDir "$LOCALAPPDATA\Programs\@ID@"
RequestExecutionLevel user
SetCompressor /SOLID lzma
CRCCheck force
ShowInstDetails show
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles
Section
  SetOutPath "$INSTDIR"
  File /r "release/win-unpacked/*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortCut "$SMPROGRAMS\@NAME@ (@ID@).lnk" "$INSTDIR\@ID@.exe"
  CreateShortCut "$DESKTOP\@NAME@ (@ID@).lnk" "$INSTDIR\@ID@.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\@ID@" "DisplayName" "@NAME@"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\@ID@" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  @INSTALL_RECEIPT@
SectionEnd
Section "Uninstall"
  !include "remove-files.nsh"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$SMPROGRAMS\@NAME@ (@ID@).lnk"
  Delete "$DESKTOP\@NAME@ (@ID@).lnk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\@ID@"
  RMDir "$INSTDIR"
SectionEnd
