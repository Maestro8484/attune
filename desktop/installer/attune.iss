; Attune Windows installer (Inno Setup 6).
;
; Packages the one-dir PyInstaller build produced by desktop\build.py (+ optionally
; desktop\package.py) into a per-user installer. Per-user by design: PrivilegesRequired
; =lowest means no UAC prompt and no admin rights needed, installing under
; {localappdata}\Programs\Attune, same tier as most modern desktop-app installers
; (VS Code, Slack, Discord all default here for the same reason: no admin needed).
;
; Build with:
;   "C:\...\Inno Setup 6\ISCC.exe" desktop\installer\attune.iss
;   (or, preferably, via desktop\build_installer.py, which also lets you override
;   the source dir and output dir without touching this file)
;
; SourceDir default. This .iss lives in the WORKTREE at:
;   attune-installer-wt\desktop\installer\attune.iss
; but the actual PyInstaller output (built once, by hand, in the main checkout) lives at:
;   attune\dist\Attune
; Both "attune-installer-wt" and "attune" are sibling directories under the same
; workspace root (...\Attune\). So the default SourceDir walks up three levels from
; this .iss's own directory (installer -> desktop -> attune-installer-wt -> workspace
; root) and back down into the MAIN checkout's dist folder:
;   ..\..\..\attune\dist\Attune
; Override at compile time for a different checkout or output layout:
;   ISCC attune.iss /DSourceDir=C:\path\to\dist\Attune
#ifndef SourceDir
  #define SourceDir SourcePath + "..\..\..\attune\dist\Attune"
#endif
#ifndef OutputDirOverride
  #define OutputDirOverride ""
#endif

#define AppName "Attune"
#define AppVersion "0.1.0"
#define AppPublisher "Joe Schmidt"
#define AppURL "https://github.com/Maestro8484/attune"
#define AppExeName "Attune.exe"

[Setup]
; Fixed once, hardcoded forever: this is what Windows uses to recognize "the same
; program" across versions (upgrades install over themselves instead of side-by-side).
; Generated once via PowerShell `[guid]::NewGuid()` — never regenerate this. Written
; directly (not via a #define) because Inno's own constant syntax and the ISPP
; preprocessor's "{#name}" macro syntax both use curly braces -- routing a
; brace-wrapped GUID through a #define fights the compiler ("Unknown constant"); the
; standard Inno idiom is exactly this literal, escaped-brace form.
AppId={{516E0E96-AF35-4C73-8214-ED0CA37F0E48}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
; Per-user install: no admin rights required, no UAC prompt.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Attune
DefaultGroupName=Attune
DisableProgramGroupPage=yes
; No .ico shipped anywhere under desktop\ as of this writing (checked: no *.ico
; in the repo). Omitting SetupIconFile falls back to Inno's default icon rather
; than inventing an asset that isn't in the product. Revisit if/when one exists.
; SetupIconFile=
OutputBaseFilename=AttuneSetup-{#AppVersion}
#if OutputDirOverride != ""
  OutputDir={#OutputDirOverride}
#else
  OutputDir=Output
#endif
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The install tree is a PyInstaller one-dir bundle (thousands of small files, ~1 GB) --
; nothing here needs a 32-bit-only Program Files path, and there is no separate x64
; payload to select, so ArchitecturesInstallIn64BitMode is left at its default.
UninstallDisplayIcon={app}\{#AppExeName}
ChangesAssociations=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Both unchecked by default -- installing Attune should not silently claim a desktop
; icon or a place in Windows startup. The user opts in explicitly.
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "startupicon"; Description: "Start Attune when Windows starts"; Flags: unchecked

[Files]
; The ENTIRE one-dir build tree: Attune.exe, _internal\ (its PyInstaller runtime),
; and analyzer\ (AttuneAnalyzer.exe + its own runtime) -- see desktop\build.py for why
; the analyzer is a nested second program rather than folded into the same bundle.
; recursesubdirs + createallsubdirdirs preserves the whole layout unchanged.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Attune"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall Attune"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Attune"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; "Start Attune when Windows starts" -- a per-user HKCU Run value, never HKLM (no admin
; rights, matches PrivilegesRequired=lowest). Inno auto-removes anything it wrote to the
; registry on uninstall, so this key disappears with the app; no [UninstallDelete] needed.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "Attune"; ValueData: """{app}\{#AppExeName}"""; \
    Flags: uninsdeletevalue; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch Attune"; Flags: nowait postinstall skipifsilent

; Deliberately NO [UninstallDelete] section. User data lives in
; %APPDATA%\Attune\settings.json (src\config.py) and mixer.db lives wherever the user
; pointed it (default {app}\mixer.db only if desktop\package.py folded one in). Inno's
; default uninstall behavior already removes only what [Files]/[Icons]/[Registry] put
; down -- the install directory tree, the Start Menu group, the optional desktop icon,
; and the Run key -- and never touches %APPDATA%, so there is nothing to add here. The
; absence of an [UninstallDelete] section IS the "preserve user data" design; do not
; add one that reaches into {userappdata}\Attune.
