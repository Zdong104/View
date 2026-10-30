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
// and wait), wait, stop (force), or resident (only a View in the background:
// no window, just its tray icon).  Returns how many View processes with a
// window open (close, wait) or View processes (stop) were left, or how many
// were stopped (resident).
// View is asked to quit with WM_CLOSE to the hidden window of its tray icon
// (preview/tray.py: like Quit in its menu, a document with unsaved changes
// asks).  Not with taskkill without /F: that closes only the front window of
// a View with windows open (which then stays in the background), and reaches
// the tray window only when no window is open.  A View with no window open has
// nothing to ask, so it is not waited for long.
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
    'function HasWindow($x) { $p = Get-Process -Id $x.ProcessId -ErrorAction SilentlyContinue' + #13#10 +
    '  [bool]($p -and $p.MainWindowHandle -ne [IntPtr]::Zero) }' + #13#10 +
    'function Shown { Gui | Where-Object { HasWindow $_ } }' + #13#10 +
    'Add-Type -Namespace ViewSetup -Name User32 -MemberDefinition (''[DllImport("user32.dll", CharSet = CharSet.Unicode)] '' +' + #13#10 +
    '  ''public static extern IntPtr FindWindowEx(IntPtr p, IntPtr a, string c, IntPtr t); '' +' + #13#10 +
    '  ''[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p); '' +' + #13#10 +
    '  ''[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);'')' + #13#10 +
    '# Quit (WM_CLOSE to the tray window); a View without one (an older version): taskkill' + #13#10 +
    'function Ask($x) { $h = [IntPtr]::Zero; $sent = $false' + #13#10 +
    '  while (($h = [ViewSetup.User32]::FindWindowEx([IntPtr]::Zero, $h, ''ViewTrayWindow'', [IntPtr]::Zero)) -ne [IntPtr]::Zero) {' + #13#10 +
    '    $id = [uint32]0; [void][ViewSetup.User32]::GetWindowThreadProcessId($h, [ref]$id)' + #13#10 +
    '    if ($id -eq $x.ProcessId) { $sent = [ViewSetup.User32]::PostMessage($h, 0x10, [IntPtr]::Zero, [IntPtr]::Zero) } }' + #13#10 +
    '  if (-not $sent) { taskkill.exe /PID $x.ProcessId 2>&1 | Out-Null } }' + #13#10 +
    'if ($Mode -eq ''resident'') {' + #13#10 +
    '  $r = @(Gui | Where-Object { -not (HasWindow $_) })' + #13#10 +
    '  $r | ForEach-Object { Ask $_ }' + #13#10 +
    '  $r | ForEach-Object { Wait-Process -Id $_.ProcessId -Timeout 5 -ErrorAction SilentlyContinue }' + #13#10 +
    '  $r | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }' + #13#10 +
    '  exit $r.Count }' + #13#10 +
    'if ($Mode -eq ''stop'') {' + #13#10 +
    '  $p = @(All)' + #13#10 +
    '  $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }' + #13#10 +
    '  $p | ForEach-Object { Wait-Process -Id $_.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }' + #13#10 +
    '  exit @(All).Count }' + #13#10 +
    '# close the windows (and quit), like the user would' + #13#10 +
    '$asked = @{}' + #13#10 +
    'if ($Mode -eq ''close'') { Gui | ForEach-Object { $asked[$_.ProcessId] = 1; Ask $_ } }' + #13#10 +
    '$end = (Get-Date).AddSeconds(15)' + #13#10 +
    '$quiet = (Get-Date).AddSeconds(3)' + #13#10 +
    'while (@(Gui).Count -and (Get-Date) -lt $end -and (@(Shown).Count -or (Get-Date) -lt $quiet)) {' + #13#10 +
    '  # (wait: the user closed the windows, and View stayed in the background)' + #13#10 +
    '  Gui | Where-Object { -not $asked[$_.ProcessId] -and -not (HasWindow $_) } | ForEach-Object {' + #13#10 +
    '    $asked[$_.ProcessId] = 1; Ask $_ }' + #13#10 +
    '  Start-Sleep -Milliseconds 300 }' + #13#10 +
    'exit @(Shown).Count' + #13#10, False);
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
  // View in the background (tray icon only) has no window for Restart Manager
  // to close; one with windows open is still left to Restart Manager
  Log(Format('Stopped %d View(s) running in the background', [StopView('resident')]));
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
