; Inno Setup script for ButterPDF.
;
; Build:  iscc packaging\installer.iss
; Output: packaging\out\ButterPDF-Setup-0.1.0.exe
;
; Interactive:  double-click, Next, Install, done.
; Silent (GPO or any deployment tool):
;     ButterPDF-Setup-0.1.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
; Silent uninstall:
;     "%ProgramFiles%\ButterPDF\unins000.exe" /VERYSILENT /NORESTART

#define AppName "ButterPDF"
#define AppVersion "0.1.0"
#define AppPublisher "ButterPDF"
#define AppExeName "ButterPDF.exe"

[Setup]
; AppId must never change across versions -- it is how Windows recognises an
; upgrade rather than a second parallel install. Generate one GUID, once, and
; leave it alone forever.
AppId={{8F3C21A4-6D5E-4B7A-9E14-2C7B5A9D3E60}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=out
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; admin installs to Program Files for all users. Switch to "lowest" and
; {autopf}->{localappdata} if you need to deploy without admin rights, e.g.
; to non-persistent desktops where users aren't local admins.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; Uninstall entry in Apps & Features
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}

; Unsigned installers trigger SmartScreen. Sign both the EXE and this installer
; if you're deploying to anyone but yourself -- see docs/PACKAGING.md.
; SignTool=mysigntool

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; \
    GroupDescription: "Shortcuts:"

; Taking over the .pdf association is NOT a default. On a managed fleet this
; silently changes what opens when 70 people double-click an invoice, and the
; first thing they notice is that their PDF reader "broke". Opt-in only, and
; deliberately unchecked.
Name: "associate"; Description: "Make {#AppName} the default app for .pdf files"; \
    GroupDescription: "File associations:"; Flags: unchecked

; Single-letter tool shortcuts are off by default already; this LOCKS them
; off so users can't turn them on (the Tools menu shows "set by
; administrator"). Silent install: add /TASKS="lockletterkeys".
Name: "lockletterkeys"; Description: "Lock single-letter tool shortcuts off for all users"; \
    GroupDescription: "Administrator policy:"; Flags: unchecked

[Files]
; PyInstaller onedir output. Everything under dist\ButterPDF\.
Source: "..\dist\ButterPDF\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\ButterPDF\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; Always register the app as a *capable* PDF handler, so it shows up under
; "Open with" without becoming the default.
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".pdf"; ValueData: ""; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; \
    Flags: uninsdeletekey

; Only if the user opted in.
Root: HKA; Subkey: "Software\Classes\ButterPDF.Document"; \
    ValueType: string; ValueName: ""; ValueData: "PDF Document"; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\ButterPDF.Document\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\ButterPDF.Document\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.pdf"; \
    ValueType: string; ValueName: ""; ValueData: "ButterPDF.Document"; \
    Flags: uninsdeletevalue; Tasks: associate

; Policy lives under HKLM\Software\Policies so GPO or deployment tooling can set the same
; value without the installer. See docs/PACKAGING.md.
Root: HKLM; Subkey: "Software\Policies\{#AppName}"; ValueType: dword; \
    ValueName: "SingleKeyShortcuts"; ValueData: 0; Flags: uninsdeletekey; Tasks: lockletterkeys

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent
