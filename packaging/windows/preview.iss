; Inno Setup script for Preview (Windows).
;   iscc /DAppVersion=0.1.0 /DSourceDir=..\..\dist\Preview /DOutputDir=..\..\dist packaging\windows\preview.iss
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\Preview"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
AppId={{6C1E2B0E-3D4A-4C55-9C1B-2F1F5E0A7D11}
AppName=Preview
AppVersion={#AppVersion}
AppVerName=Preview {#AppVersion}
AppPublisher=Zdong104
AppPublisherURL=https://github.com/Zdong104/Preview
AppSupportURL=https://github.com/Zdong104/Preview/issues
AppUpdatesURL=https://github.com/Zdong104/Preview/releases
DefaultDirName={autopf}\Preview
DefaultGroupName=Preview
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=Preview-Setup-{#AppVersion}
SetupIconFile=..\..\build\preview.ico
UninstallDisplayIcon={app}\Preview.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
ChangesAssociations=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Preview"; Filename: "{app}\Preview.exe"
Name: "{group}\Uninstall Preview"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Preview"; Filename: "{app}\Preview.exe"; Tasks: desktopicon

[Registry]
; "Open with" entries (does not take over any default association)
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "Preview"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\shell\open\command"; ValueType: string; ValueData: """{app}\Preview.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".png"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".jpg"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".jpeg"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".webp"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".gif"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".tif"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".tiff"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".stl"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".obj"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".csv"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".mp4"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".mkv"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Preview.exe\SupportedTypes"; ValueType: string; ValueName: ".webm"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithList\Preview.exe"; ValueType: none; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.png\OpenWithList\Preview.exe"; ValueType: none; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.jpg\OpenWithList\Preview.exe"; ValueType: none; Flags: uninsdeletekey

[Run]
Filename: "{app}\Preview.exe"; Description: "{cm:LaunchProgram,Preview}"; Flags: nowait postinstall skipifsilent
