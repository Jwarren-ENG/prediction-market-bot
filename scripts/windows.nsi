Unicode true
Name "Prediction Market Bot"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\Prediction Market Bot"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "${APPICON}"
ShowInstDetails show
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_HEADER_TEXT "Microsoft component terms"
!define MUI_PAGE_HEADER_SUBTEXT "Review the terms for the Microsoft components included with this Windows app."
!define MUI_LICENSEPAGE_TEXT_TOP "Review the terms for Microsoft Visual C++ and Microsoft Edge WebView2 below."
!define MUI_LICENSEPAGE_CHECKBOX
!define MUI_LICENSEPAGE_CHECKBOX_TEXT "I accept these Microsoft component terms."
!insertmacro MUI_PAGE_LICENSE "${COMPONENT_TERMS}"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\PMB.exe"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

!define APPKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\PMBResearchDesk"
!define WEBVIEWKEY "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

; Interactive installs use the unchecked license-page checkbox. For authorized
; unattended installations use /S /ACCEPTCOMPONENTTERMS after reviewing the terms.
Function .onInit
  IfSilent 0 done
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/ACCEPTCOMPONENTTERMS" $R1
  IfErrors refused
  StrCmp $R1 "" done refused
refused:
  SetErrorLevel 3
  Abort
done:
FunctionEnd

Section "Paper research desktop"
  SetShellVarContext current
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/WORKERONLY" $R1
  IfErrors checkWebView haveWebView
checkWebView:
  SetRegView 32
  ReadRegStr $0 HKLM "${WEBVIEWKEY}" "pv"
  ${If} $0 == ""
  ${OrIf} $0 == "0.0.0.0"
    ReadRegStr $0 HKCU "${WEBVIEWKEY}" "pv"
  ${EndIf}
  ${If} $0 == ""
  ${OrIf} $0 == "0.0.0.0"
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=WebView2Setup.exe "${WEBVIEW}"
    DetailPrint "Installing Microsoft Edge WebView2 for the desktop interface"
    ClearErrors
    ExecWait '"$PLUGINSDIR\WebView2Setup.exe" /silent /install' $0
    ${If} ${Errors}
    ${OrIf} $0 != 0
      MessageBox MB_ICONSTOP|MB_OK "Microsoft WebView2 could not be installed. Install the Evergreen WebView2 Runtime and run this installer again." /SD IDOK
      SetErrorLevel 2
      Abort
    ${EndIf}
  ${EndIf}
haveWebView:
  IfFileExists "$INSTDIR\payload\pmb.py" 0 +2
    ExecWait '"$INSTDIR\payload\runtime\python.exe" -I -B "$INSTDIR\payload\pmb.py" stop --matching-root'
  SetOutPath "$INSTDIR"
  SetOverwrite on
  File /r "${STAGE}\*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\Prediction Market Bot"
  CreateShortCut "$SMPROGRAMS\Prediction Market Bot\Prediction Market Bot.lnk" "$INSTDIR\PMB.exe"
  WriteRegStr HKCU "${APPKEY}" "DisplayName" "Prediction Market Bot"
  WriteRegStr HKCU "${APPKEY}" "DisplayVersion" "${APPVERSION}"
  WriteRegStr HKCU "${APPKEY}" "Publisher" "Prediction Market Bot"
  WriteRegStr HKCU "${APPKEY}" "DisplayIcon" "$INSTDIR\PMB.exe"
  WriteRegStr HKCU "${APPKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  ExecWait '"$INSTDIR\payload\runtime\python.exe" -I -B "$INSTDIR\payload\background.py" remove'
  ExecWait '"$INSTDIR\payload\runtime\python.exe" -I -B "$INSTDIR\payload\pmb.py" stop --matching-root'
  Delete "$SMPROGRAMS\Prediction Market Bot\Prediction Market Bot.lnk"
  RMDir "$SMPROGRAMS\Prediction Market Bot"
  DeleteRegKey HKCU "${APPKEY}"
  !include "${OWNED}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
