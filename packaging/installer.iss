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

; No "make it the default for .pdf" task. Windows 10/11 ignore an installer
; setting the default (a hash-protected UserChoice wins), so the old task did
; nothing for most people while claiming otherwise. The app registers itself
; as a PDF handler instead (below); the user picks it in Settings > Default
; apps, or IT sets it by GPO -- docs/PACKAGING.md, "Default PDF app".

; No task for the single-letter shortcut policy (removed, user's call): the
; shortcuts are off by default, which is enough. IT can still lock them
; through HKLM\Software\Policies\SilverbowPDF by GPO -- see docs/PACKAGING.md.

[InstallDelete]
; An upgrade starts from a clean bundle: [Files] only adds and replaces, so
; DLLs and modules a newer build no longer has would linger beside it.
Type: filesandordirs; Name: "{app}\_internal"

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

; A ProgID, offered for .pdf (OpenWithProgids) and listed under Settings >
; Default apps (Capabilities + RegisteredApplications) -- the documented way
; to be a choosable handler. Never written: .pdf's own default, so nothing
; changes until the user or IT picks this app, and uninstalling removes only
; what's ours (the old task deleted .pdf's default even if another app owned
; it by then).
Root: HKA; Subkey: "Software\Classes\SilverbowPDF.Document"; \
    ValueType: string; ValueName: ""; ValueData: "PDF Document"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\SilverbowPDF.Document\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"
Root: HKA; Subkey: "Software\Classes\SilverbowPDF.Document\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; \
    ValueType: string; ValueName: "SilverbowPDF.Document"; ValueData: ""; \
    Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\SilverbowPDF"; Flags: uninsdeletekeyifempty
Root: HKA; Subkey: "Software\SilverbowPDF\Capabilities"; \
    ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"; \
    Flags: uninsdeletekey
Root: HKA; Subkey: "Software\SilverbowPDF\Capabilities"; \
    ValueType: string; ValueName: "ApplicationDescription"; \
    ValueData: "View, sign and edit PDF documents"
Root: HKA; Subkey: "Software\SilverbowPDF\Capabilities\FileAssociations"; \
    ValueType: string; ValueName: ".pdf"; ValueData: "SilverbowPDF.Document"
Root: HKA; Subkey: "Software\RegisteredApplications"; \
    ValueType: string; ValueName: "{#AppName}"; \
    ValueData: "Software\SilverbowPDF\Capabilities"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent
