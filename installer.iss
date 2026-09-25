; Inno Setup script for PDF Workbench.
;
; Build:  iscc packaging\installer.iss
; Output: packaging\out\PDFWorkbench-Setup-0.1.0.exe
;
; Interactive:  double-click, Next, Install, done.
; Silent (GPO or any deployment tool):
;     PDFWorkbench-Setup-0.1.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
; Silent uninstall:
;     "%ProgramFiles%\PDF Workbench\unins000.exe" /VERYSILENT /NORESTART

#define AppName "PDF Workbench"
#define AppVersion "0.1.0"
#define AppPublisher "PDF Workbench"
#define AppExeName "PDFWorkbench.exe"

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

[Files]
; PyInstaller onedir output. Everything under dist\PDFWorkbench\.
Source: "..\dist\PDFWorkbench\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\PDFWorkbench\*"; DestDir: "{app}"; \
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
Root: HKA; Subkey: "Software\Classes\PDFWorkbench.Document"; \
    ValueType: string; ValueName: ""; ValueData: "PDF Document"; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\PDFWorkbench.Document\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\PDFWorkbench.Document\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.pdf"; \
    ValueType: string; ValueName: ""; ValueData: "PDFWorkbench.Document"; \
    Flags: uninsdeletevalue; Tasks: associate

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent
