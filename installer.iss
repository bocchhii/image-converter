; Inno Setup script - builds MasterConverter-Setup.exe
#define MyAppName "Master Converter"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppExe "MasterConverter.exe"

[Setup]
; same AppId as when the app was called "Image Converter", so installing this
; upgrades an old install instead of adding a second copy
AppId={{B7D2F3A1-5C4E-4E7A-9F11-3A6C2D8E4B90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; installs just for the current user by default, so no admin password is needed
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=installer_output
OutputBaseFilename=MasterConverter-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; in-app updates run this silently: close the running app first, but don't let Windows
; restart it - the [Run] entry below starts the new version (just once)
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
; left over from when the app was called "Image Converter"
Type: files; Name: "{app}\ImageConverter.exe"
Type: files; Name: "{autoprograms}\Image Converter.lnk"
Type: files; Name: "{autodesktop}\Image Converter.lnk"

[Files]
Source: "dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
; after a silent install (the in-app update), start the new version straight away
Filename: "{app}\{#MyAppExe}"; Flags: nowait; Check: WizardSilent
