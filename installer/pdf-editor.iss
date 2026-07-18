; Inno Setup script for PDF Editor — per-user install + PDF handler registration.
;
; Build via scripts\build_installer.ps1 (which passes the version through /D and
; runs ISCC over an already-built dist\pdf-editor\ onedir bundle). Requires
; Inno Setup 6.3+ (winget install -e --id JRSoftware.InnoSetup).
;
; Design notes (see CLAUDE.md "Installer" section for the full rationale):
;  - Per-user (PrivilegesRequired=lowest): registry lands in HKCU\Software\Classes
;    via Root: HKA; no admin / UAC prompt.
;  - The AppId GUID below is STABLE and must NEVER change — it is what makes a
;    newer installer upgrade the existing install in place instead of creating a
;    second parallel copy, and it is the uninstall key.
;  - Registers the app as an AVAILABLE PDF handler only. Windows 10/11 forbid
;    setting the default handler programmatically (UserChoice hash) — we never
;    touch .pdf's default value; a post-install [Run] deep-links to Settings.

#define AppName "PDF Editor"
#define AppPublisher "PDF Editor"
#define AppExeName "pdf-editor.exe"
#define ProgId "PDFEditor.Document"

; AppVersion is normally supplied by the build script: ISCC /DAppVersion=x.y.z
; The fallback keeps a direct `iscc pdf-editor.iss` from failing.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; STABLE — do not change this GUID, ever (see notes above).
AppId={{9E95EBB5-DFF7-4DC4-9EC3-1610D5C4FA44}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}

; Per-user install, no admin / UAC. HKA -> HKCU, {autopf} -> {localappdata}\Programs,
; {autoprograms} -> the per-user Start menu.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
; Version-INDEPENDENT path so upgrades overwrite in place (modern per-user apps
; convention, e.g. VS Code).
DefaultDirName={localappdata}\Programs\PDF Editor
DisableProgramGroupPage=yes

; Inno broadcasts SHCNE_ASSOCCHANGED at end of install AND uninstall so Explorer
; refreshes the icon cache — no hand-rolled SHChangeNotify needed.
ChangesAssociations=yes
; Restart-Manager-closes a running pdf-editor.exe during an upgrade.
CloseApplications=yes

; Requires Inno Setup 6.3+ (x64compatible token). The app is 64-bit Python.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

OutputDir=..\dist
OutputBaseFilename=pdf-editor-setup-{#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller onedir bundle (pdf-editor.exe + _internal\).
Source: "..\dist\pdf-editor\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; App icon (Start-menu shortcut). The exe already embeds it; ship the file so
; the shortcut's IconFilename resolves to a persisted installed file.
Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
; DISTINCT PDF file-type icon — the ProgID DefaultIcon shown on .pdf files in
; Explorer (once we're the default). Separate from the app icon above.
Source: "..\assets\pdf-document.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; Wipe the previous onedir payload BEFORE copying so stale DLLs from an older
; version can't linger on upgrade. Scoped to _internal so nothing user-placed
; in {app} is nuked; the exe + icon are replaced by normal overwrite.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Registry]
; --- ProgID (ours only): uninsdeletekey on the root removes the whole subtree ---
Root: HKA; Subkey: "Software\Classes\{#ProgId}"; ValueType: string; ValueData: "PDF Document"; Flags: uninsdeletekey
; DefaultIcon = the DISTINCT file-type icon (pdf-document.ico), NOT the app icon.
Root: HKA; Subkey: "Software\Classes\{#ProgId}\DefaultIcon"; ValueType: string; ValueData: "{app}\pdf-document.ico,0"
; FriendlyAppName — a PyInstaller windowed exe has no version resource, so without
; this the "Open with" entry would read "pdf-editor.exe".
Root: HKA; Subkey: "Software\Classes\{#ProgId}\shell\open"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"
Root: HKA; Subkey: "Software\Classes\{#ProgId}\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""

; --- Capabilities (ours only): uninsdeletekey on Software\PDFEditor removes it all ---
Root: HKA; Subkey: "Software\PDFEditor"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\PDFEditor\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"
Root: HKA; Subkey: "Software\PDFEditor\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Open, view and edit PDF files"
Root: HKA; Subkey: "Software\PDFEditor\Capabilities"; ValueType: string; ValueName: "ApplicationIcon"; ValueData: "{app}\icon.ico,0"
Root: HKA; Subkey: "Software\PDFEditor\Capabilities\FileAssociations"; ValueType: string; ValueName: ".pdf"; ValueData: "{#ProgId}"

; --- RegisteredApplications (SHARED key): uninsdeletevalue removes ONLY our value ---
; The value name here must exactly match the ms-settings deep-link registeredAppUser.
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#AppName}"; ValueData: "Software\PDFEditor\Capabilities"; Flags: uninsdeletevalue

; --- Open-with surfacing (SHARED key): uninsdeletevalue removes ONLY our value.
; We do NOT set .pdf's default value — that would be default-seizing (forbidden).
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgIds"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue

[Run]
; Honest "set as default": we can't seize it programmatically, so deep-link to
; THIS app's page in Settings > Default apps (Win11; falls back to the generic
; page on older builds). Post-install checkbox, off in silent installs.
Filename: "ms-settings:defaultapps?registeredAppUser=PDF%20Editor"; Description: "Set {#AppName} as the default PDF app"; Flags: shellexec nowait postinstall skipifsilent
