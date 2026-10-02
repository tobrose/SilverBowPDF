; Inno Setup script for SilverbowPDF.
;
; Build:  iscc packaging\installer.iss
; Output: packaging\out\SilverbowPDF-Setup-<version>.exe
;
; Interactive:  double-click, Next, Install, done.
; Silent (GPO or any deployment tool):
;     SilverbowPDF-Setup-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
; Silent uninstall:
;     "%ProgramFiles%\SilverbowPDF\unins000.exe" /VERYSILENT /NORESTART

#define AppName "SilverbowPDF"
; build.ps1 passes /DAppVersion=<pdfapp.__version__>; this is only the
; fallback for compiling by hand.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#define AppPublisher "SilverbowPDF"
#define AppExeName "SilverbowPDF.exe"

[Setup]
; AppId must never change across versions -- it is how Windows recognises an
; upgrade rather than a second parallel install. Generate one GUID, once, and
; leave it alone forever. (Replaced once, at the rename from ButterPDF, while
; nothing was deployed that needed upgrading.)
AppId={{06C43E6D-74E9-4B84-87AC-61182901A507}
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
SetupIconFile=icon.ico
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

; No task for the single-letter shortcut policy (removed, user's call): the
; shortcuts are off by default, which is enough. IT can still lock them
; through HKLM\Software\Policies\SilverbowPDF by GPO -- see docs/PACKAGING.md.

[Files]
; PyInstaller onedir output. Everything under dist\SilverbowPDF\.
Source: "..\dist\SilverbowPDF\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\SilverbowPDF\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID must match the one the app sets (pdfapp/__main__.py), or a
; pinned shortcut and the running window show up as two taskbar buttons.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    AppUserModelID: "SilverbowPDF.SilverbowPDF"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    AppUserModelID: "SilverbowPDF.SilverbowPDF"; Tasks: desktopicon

[Registry]
; Always register the app as a *capable* PDF handler, so it shows up under
; "Open with" without becoming the default.
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".pdf"; ValueData: ""; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; \
    Flags: uninsdeletekey

; Only if the user opted in.
Root: HKA; Subkey: "Software\Classes\SilverbowPDF.Document"; \
    ValueType: string; ValueName: ""; ValueData: "PDF Document"; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\SilverbowPDF.Document\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\SilverbowPDF.Document\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; \
    Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.pdf"; \
    ValueType: string; ValueName: ""; ValueData: "SilverbowPDF.Document"; \
    Flags: uninsdeletevalue; Tasks: associate

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent
