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

[Code]
// Stop a running screenshot watcher (View.exe --screenshot-watcher, resident
// since 0.1.6) before its files are replaced or removed.  Restart Manager cannot
// close it (it has no window), and a silent in-app update then aborts at once.
// Not with the installed  view-app --screenshot-watcher --stop: up to 0.1.9 that
// finds no PID to stop (see screenshot._lock_owner), so match the processes.
procedure StopScreenshotWatchers;
var
  Script: String;
  Code: Integer;
begin
  Script := ExpandConstant('{tmp}\stop-watcher.ps1');
  SaveStringToFile(Script,
    'param([string]$Dir)' + #13#10 +
    '$dir = $Dir.TrimEnd(''\'') + ''\''' + #13#10 +
    '$w = @(Get-CimInstance Win32_Process -Filter "Name=''View.exe'' OR Name=''view-app.exe''" |' + #13#10 +
    '  Where-Object { $_.ExecutablePath -and $_.CommandLine -like ''*--screenshot-watcher*'' -and' + #13#10 +
    '    $_.ExecutablePath.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) })' + #13#10 +
    '$w | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }' + #13#10 +
    '$w | ForEach-Object { Wait-Process -Id $_.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }' + #13#10 +
    'exit $w.Count' + #13#10, False);
  if Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
          '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script +
          '" -Dir "' + ExpandConstant('{app}') + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Log(Format('Stopped %d screenshot watcher(s) running from %s', [Code, ExpandConstant('{app}')]))
  else
    Log(Format('Could not look for running screenshot watchers: %s', [SysErrorMessage(Code)]));
end;

// Uninstall: stop View running from {app} (its windows, helpers and screenshot
// watchers), else the files it has open stay behind ("Removed all? No").  Its
// windows first, as if the user closed them (a document with unsaved changes
// asks); what is left after a while is stopped.  Mode: close (close the windows
// and wait), wait, or stop (force).  Returns how many View windows (close,
// wait) or View processes (stop) are still running.
function StopView(const Mode: String): Integer;
var
  Script: String;
begin
  Script := ExpandConstant('{tmp}\stop-view.ps1');
  SaveStringToFile(Script,
    'param([string]$Dir, [string]$Mode)' + #13#10 +
    '$dir = $Dir.TrimEnd(''\'') + ''\''' + #13#10 +
    '# (@(...).Count: one CimInstance has no Count)' + #13#10 +
    'function All { Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and' + #13#10 +
    '  $_.ExecutablePath.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) -and $_.Name -notlike ''unins*'' } }' + #13#10 +
    'function Gui { All | Where-Object { $_.Name -in ''View.exe'', ''view-app.exe'' -and' + #13#10 +
    '  $_.CommandLine -notlike ''*--screenshot-watcher*'' } }' + #13#10 +
    'if ($Mode -eq ''stop'') {' + #13#10 +
    '  $p = @(All)' + #13#10 +
    '  $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }' + #13#10 +
    '  $p | ForEach-Object { Wait-Process -Id $_.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }' + #13#10 +
    '  exit @(All).Count }' + #13#10 +
    '# without /F taskkill closes the windows (WM_CLOSE), like the user would' + #13#10 +
    'if ($Mode -eq ''close'') { Gui | ForEach-Object { taskkill.exe /PID $_.ProcessId | Out-Null } }' + #13#10 +
    '$end = (Get-Date).AddSeconds(15)' + #13#10 +
    'while (@(Gui).Count -and (Get-Date) -lt $end) { Start-Sleep -Milliseconds 300 }' + #13#10 +
    'exit @(Gui).Count' + #13#10, False);
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
              '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script +
              '" -Dir "' + ExpandConstant('{app}') + '" -Mode ' + Mode, '', SW_HIDE,
              ewWaitUntilTerminated, Result) then begin
    Log(Format('Could not look for a running View: %s', [SysErrorMessage(Result)]));
    Result := 0;
  end;
end;

procedure StopRunningView;
var
  Left: Integer;
begin
  Left := StopView('close');
  while (Left > 0) and not UninstallSilent and (MsgBox('View is still open, perhaps ' +
         'asking whether to save a document.' + #13#10#13#10 + 'Close View, then click ' +
         'Retry. Cancel closes it without saving.', mbConfirmation, MB_RETRYCANCEL) = IDRETRY) do
    Left := StopView('wait');
  Log(Format('View windows still open after closing them: %d', [Left]));
  Left := StopView('stop');
  Log(Format('View processes still running from %s: %d', [ExpandConstant('{app}'), Left]));
  if (Left > 0) and not UninstallSilent then
    MsgBox('View could not be stopped, so some of its files stay in ' +
           ExpandConstant('{app}') + '. Delete that folder after closing View.', mbError, MB_OK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopScreenshotWatchers;
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningView;
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
