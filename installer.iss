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
; a log of every install in %TEMP% ("Setup Log <date> #<n>.txt"), for when something goes wrong
SetupLogging=yes

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

[Code]
{ The in-app update runs this installer silently, from the app, just before the app closes.
  The app is a one-file bundle: two processes run MasterConverter.exe (a small launcher,
  and the app itself), and they need a moment to exit. If the installer goes ahead while
  they're still running, the file can't be replaced and a silent install quietly fails (and
  the "close running apps" step can leave the launcher stuck, holding the file). So before
  any file is touched: wait for Master Converter to close by itself, and end whatever is
  still running after that - it's a leftover of the app that asked for this update. }

{ True while the installed .exe is still open (the app, or its launcher, running): the very
  thing that stops it from being replaced. Tested directly - can it be opened exclusively
  for writing? - rather than by looking for the process by name. }
function AppFileInUse(): Boolean;
var
  FileName: String;
  Stream: TFileStream;
begin
  Result := False;
  FileName := ExpandConstant('{app}\{#MyAppExe}');
  if not FileExists(FileName) then
    Exit;
  try
    Stream := TFileStream.Create(FileName, fmOpenReadWrite or fmShareExclusive);
    Stream.Free;
  except
    Result := True;
  end;
end;

function SetEnvironmentVariable(Name, Value: String): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

{ As soon as the installer starts: the app that asked for this update (a PyInstaller one-file
  app) passed its "your files are unpacked in my temp folder" variables down to this installer,
  and they'd reach the new version that [Run] starts too - which would then look for its files
  in the old app's temp folder (deleted by now) and never start. This tells PyInstaller to
  ignore them and start fresh (it only affects programs started from this installer). }
function InitializeSetup(): Boolean;
begin
  SetEnvironmentVariable('PYINSTALLER_RESET_ENVIRONMENT', '1');
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  I, ResultCode: Integer;
begin
  Result := '';
  if not WizardSilent then
    Exit;  { someone running the installer themselves: the usual "close the app" question }
  for I := 1 to 40 do  { up to 10 s for the app to close by itself }
  begin
    if not AppFileInUse() then
    begin
      Log('Master Converter has closed; installing.');
      Exit;
    end;
    Sleep(250);
  end;
  Log('Master Converter is still running after 10 s: ending it, so it can be replaced.');
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExe}', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
  for I := 1 to 20 do  { and up to 5 s more for Windows to let go of the file }
  begin
    if not AppFileInUse() then
      Exit;
    Sleep(250);
  end;
end;
