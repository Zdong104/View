; Inno Setup script for View (Windows).
;   iscc /DAppVersion=0.1.0 /DSourceDir=..\..\dist\View /DOutputDir=..\..\dist packaging\windows\preview.iss
; Signed release builds add  /DSign "/Sviewsign=pwsh -NoProfile -File <repo>\packaging\windows\sign.ps1 -Path $f"
; so the installer and its uninstaller are signed too (see sign.ps1).
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\View"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
AppId={{93A42E94-E5CF-4C0D-95B9-B60F994D4175}
AppName=View
AppVersion={#AppVersion}
AppVerName=View {#AppVersion}
AppPublisher=Zdong104
AppPublisherURL=https://github.com/Zdong104/View
AppSupportURL=https://github.com/Zdong104/View/issues
AppUpdatesURL=https://github.com/Zdong104/View/releases
DefaultDirName={autopf}\View
DefaultGroupName=View
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=View-Setup-{#AppVersion}
SetupIconFile=..\..\build\preview.ico
UninstallDisplayIcon={app}\View.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
ChangesAssociations=yes
#ifdef Sign
SignTool=viewsign
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\View"; Filename: "{app}\View.exe"; AppUserModelID: "Zdong104.View"
Name: "{group}\Uninstall View"; Filename: "{uninstallexe}"
Name: "{autodesktop}\View"; Filename: "{app}\View.exe"; AppUserModelID: "Zdong104.View"; Tasks: desktopicon

[Registry]
; "Open with" entries (does not take over any default association)
Root: HKA; Subkey: "Software\Classes\Applications\View.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "View"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\shell\open\command"; ValueType: string; ValueData: """{app}\View.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".png"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".jpg"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".jpeg"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".webp"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".gif"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".tif"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".tiff"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".stl"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".obj"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".csv"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".mp4"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".mkv"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".webm"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".md"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".markdown"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\View.exe\SupportedTypes"; ValueType: string; ValueName: ".dxf"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithList\View.exe"; ValueType: none; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.png\OpenWithList\View.exe"; ValueType: none; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.jpg\OpenWithList\View.exe"; ValueType: none; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.dxf\OpenWithList\View.exe"; ValueType: none; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.md\OpenWithList\View.exe"; ValueType: none; Flags: uninsdeletekey
; Screenshot quick edit: the watcher adds this login entry itself (on by default,
; View > Preferences turns it off); only removed here, on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "View Screenshot Editor"; Flags: dontcreatekey uninsdeletevalue

[Run]
Filename: "{app}\View.exe"; Description: "{cm:LaunchProgram,View}"; Flags: nowait postinstall skipifsilent
; In-app updates (preview/updates.py) run  View-Setup-<v>.exe /SILENT ... /RELAUNCH
; and expect View to start again afterwards, as the user, not elevated.
Filename: "{app}\View.exe"; Flags: nowait runasoriginaluser; Check: RelaunchRequested
; Screenshot quick edit: start watching Pictures\Screenshots now (it exits at
; once if the user turned the feature off, and registers its login entry if not).
Filename: "{app}\View.exe"; Parameters: "--screenshot-watcher"; Flags: nowait runasoriginaluser

[UninstallRun]
Filename: "{app}\view-app.exe"; Parameters: "--screenshot-watcher --stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopScreenshotWatcher"

[Code]
// Stop a running screenshot watcher before its files are replaced (an
// older View without the feature just exits on the unknown option).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  if FileExists(ExpandConstant('{app}\view-app.exe')) then
    Exec(ExpandConstant('{app}\view-app.exe'), '--screenshot-watcher --stop', '',
         SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

function RelaunchRequested: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/RELAUNCH') = 0 then
      Result := True;
end;
