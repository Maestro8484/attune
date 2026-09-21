; Attune Windows installer (Inno Setup 6).
;
; Packages the one-dir PyInstaller build produced by desktop\build.py into a per-user
; installer. Per-user by design: PrivilegesRequired=lowest means no UAC prompt and no
; admin rights needed, installing under {localappdata}\Programs\Attune, same tier as
; most modern desktop-app installers (VS Code, Slack, Discord all default here for the
; same reason: no admin needed).
;
; Build with:
;   python desktop\build_installer.py --dist <path to dist\Attune> --out <staging dir>
;   or, for the whole release set (installer + portable zip + checksums):
;   python tools\make_release.py --dist <path to dist\Attune> --out <staging dir>
; A bare "ISCC.exe desktop\installer\attune.iss" also works and uses the defaults below.
;
; SourceDir default. SourcePath is this .iss file's own directory WITH a trailing
; backslash (observed from ISPP 6.7.3, 2026-09-20), so walking up two levels lands on
; the repo root and back down into its build output:
;   desktop\installer\  ->  desktop\  ->  <repo root>  ->  dist\Attune
; That resolves inside ANY clone of this repository. The previous default walked up
; three levels and back down into a sibling folder that had to be named "attune", which
; only worked by accident of one machine's workspace layout.
; Override at compile time for a build that lives elsewhere:
;   ISCC attune.iss /DSourceDir=C:\path\to\dist\Attune
#ifndef SourceDir
  #define SourceDir SourcePath + "..\..\dist\Attune"
#endif
#ifndef OutputDirOverride
  #define OutputDirOverride ""
#endif

; Version: ONE source of truth, the VERSION file at the repo root. Read here directly so
; a bare ISCC run is still correctly stamped; build_installer.py and make_release.py pass
; /DAppVersion=<v> read from the same file, which this #ifndef lets them override.
#ifndef AppVersion
  ; FileOpen returns 0 when the file is not there, and FileRead(0) aborts the compile
  ; with ISPP's own "Invalid file handle", which says nothing useful. Check the handle
  ; first so a missing VERSION file gets an error naming the actual problem.
  #define VersionFileHandle FileOpen(SourcePath + "..\..\VERSION")
  #if VersionFileHandle == 0
    #error VERSION file not found at the repository root. It holds the one version number this installer is stamped with.
  #endif
  #define AppVersion Trim(FileRead(VersionFileHandle))
  #expr FileClose(VersionFileHandle)
  #if AppVersion == ""
    #error VERSION file at the repository root is empty; cannot stamp this installer.
  #endif
#endif

; Hard stop, at the compiler boundary rather than only in the Python drivers: a build
; folder that has been through desktop\package.py carries a copy of the packager's
; personal library database. The [Files] Excludes below would silently leave it out;
; this makes the far likelier case loud instead, because silently shipping a nearly
; correct installer from a poisoned folder is worse than refusing to compile.
#if FileExists(SourceDir + "\mixer.db")
  #error The build folder contains mixer.db. That is desktop\package.py's PRIVATE test bundle, not a release. Point /DSourceDir at a clean build.
#endif

; Longest path inside the build tree, relative to its root. desktop\build_installer.py
; measures the folder it is packaging and passes it in; the fallback is what the 2026-09-20
; build measured, for a bare ISCC run. Used by the [Code] guard at the bottom of this file.
#ifndef LongestRelPath
  #define LongestRelPath "119"
#endif

#define AppName "Attune"
#define AppPublisher "Joe Schmidt"
#define AppURL "https://github.com/Maestro8484/attune"
#define AppExeName "Attune.exe"

[Setup]
; Fixed once, hardcoded forever: this is what Windows uses to recognize "the same
; program" across versions (upgrades install over themselves instead of side-by-side).
; Generated once via PowerShell `[guid]::NewGuid()` -- never regenerate this. Written
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
; attune.ico (next to this .iss) is a PLACEHOLDER, not final art -- that call is the
; operator's. Derived from the app's own "bee" favicon, the inline data:image/svg+xml
; at web/static/studio.html:10 (a dark rounded square, #1a1c20, with the bee-accent
; blue, #3d84c6, paired-eighth-note glyph) -- decoded and rasterized with ImageMagick's
; built-in rsvg delegate (already on this machine; nothing installed for this), packed
; multi-resolution 256/64/48/32/16, 32bpp with alpha. Verified by parsing the .ico back
; with a standalone struct-based reader (independent of ImageMagick's own `identify`):
; 5 entries, exactly those five sizes, 38118 bytes total. Revisit when real art exists.
SetupIconFile=attune.ico
OutputBaseFilename=AttuneSetup-{#AppVersion}
; Without this the compiled setup exe's own file Properties in Explorer read 0.0.0.0,
; which is what Inno defaults to, beside an AppVersion that says something else.
VersionInfoVersion={#AppVersion}
#if OutputDirOverride != ""
  OutputDir={#OutputDirOverride}
#else
  OutputDir=Output
#endif
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The install tree is a PyInstaller one-dir bundle (thousands of small files) -- nothing
; here needs a 32-bit-only Program Files path, and there is no separate x64 payload to
; select, so ArchitecturesInstallIn64BitMode is left at its default.
UninstallDisplayIcon={app}\{#AppExeName}
ChangesAssociations=no
SetupLogging=yes
; Both of these are stated rather than left to their defaults, because the [InstallDelete]
; section below depends on them. CloseApplications=yes (already the default) lets Windows'
; Restart Manager ask the user to close a running Attune, so the old _internal\ tree is not
; locked when Setup tries to remove it. RestartApplications=no overrides the default: Setup
; should not silently relaunch an app it closed, least of all during a silent install.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Both unchecked by default -- installing Attune should not silently claim a desktop
; icon or a place in Windows startup. The user opts in explicitly.
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "startupicon"; Description: "Start Attune when Windows starts"; Flags: unchecked

[InstallDelete]
; Clean upgrade. Inno's [Files] only ADDS and OVERWRITES; it never removes a file that
; the new version no longer ships. In a PyInstaller one-dir tree of ~4,000 files that is
; a real hazard: a stale module left behind by an older version stays importable by the
; newer one and wins on sys.path order. So wipe the two generated runtime trees first.
; [InstallDelete] runs after the user confirms and BEFORE any file is copied, which is
; exactly the window we want.
; Scope is deliberate and narrow: _internal\ and analyzer\ are produced wholly by
; desktop\build.py and hold nothing a user would have put there. {app} itself is NOT
; wiped, because the uninstaller (unins000.exe / unins000.dat) lives there and deleting
; it mid-upgrade would orphan the install in Add/Remove Programs.
;
; KNOWN TRADE-OFF, taken deliberately (RELENG 2026-09-20, raised by the Codex audit):
; Inno does not restore [InstallDelete] deletions if the user cancels an upgrade or it
; fails partway. So a cancelled upgrade leaves a non-working install that has to be
; installed again. That is accepted, because the alternative is worse in kind: without
; this section a stale module from an older version stays on disk and stays importable,
; which fails silently, forever, and looks like a bug in the app. A cancelled upgrade
; fails loudly and the fix is to run the installer again. The release notes say so.
;
; THE SAME APPLIES TO UPGRADING WHILE ATTUNE IS RUNNING (Fable audit, same day). The
; Preparing to Install page offers "Do not close the applications". Choosing it leaves
; _internal locked, so this section half-empties it and the copy then stops on the first
; DLL that is in use. Same outcome, same fix: close Attune and install again. Setting
; CloseApplications=force would not remove that choice, it only changes how the closing
; is done, so the honest answer is to say it in the release notes rather than to fight
; the wizard. A silent install never sees this page: Setup closes such applications by
; itself, which is why the /VERYSILENT proof does not exercise it.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\analyzer"

[Files]
; The ENTIRE one-dir build tree: Attune.exe, _internal\ (its PyInstaller runtime),
; and analyzer\ (AttuneAnalyzer.exe + its own runtime) -- see desktop\build.py for why
; the analyzer is a nested second program rather than folded into the same bundle.
; recursesubdirs + createallsubdirdirs preserves the whole layout unchanged.
;
; Excludes is the backstop for the database hazard. Inno matches each comma-separated
; pattern against the END of a path name unless it starts with a backslash (Inno 6 help,
; [Files] section), so these catch a library database anywhere in the tree, however the
; compiler was invoked and whatever the Python drivers did or did not check.
; The list mirrors DB_SUFFIXES in desktop\build_installer.py: three base names for a
; SQLite database, each with the three sidecar files SQLite writes beside it (-wal and
; -shm while open, -journal in rollback mode). A sidecar holds pages of the same
; database and is exactly as private as the database.
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Excludes: "*.db,*.db-wal,*.db-shm,*.db-journal,*.sqlite,*.sqlite-wal,*.sqlite-shm,*.sqlite-journal,*.sqlite3,*.sqlite3-wal,*.sqlite3-shm,*.sqlite3-journal"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

; --- LEGAL STREAM PLACEHOLDER: third-party license texts -------------------------------
; The licenses folder that satisfies the GPL obligations of the bundled components
; (rulings item 6) is owned by the LEGAL stream. When it exists in the repo, add its
; line here, e.g.:
;   Source: "{#SourcePath}..\..\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs
; Nothing is installed for it today; this comment is the agreed hand-over point.
; --- END LEGAL STREAM PLACEHOLDER ------------------------------------------------------

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
; %APPDATA%\Attune\settings.json (src\config.py) and the library database lives wherever
; the user pointed it. Inno's default uninstall behavior already removes only what
; [Files]/[Icons]/[Registry] put down -- the install directory tree, the Start Menu group,
; the optional desktop icon, and the Run key -- and never touches %APPDATA%, so there is
; nothing to add here. The absence of an [UninstallDelete] section IS the "preserve user
; data" design; do not add one that reaches into {userappdata}\Attune.

[Code]
{ Windows still refuses most file operations on paths longer than 259 characters, and
  Attune is a PyInstaller tree whose own deepest entry is about 120 of those. Installing
  into a long folder therefore dies PARTWAY THROUGH with Windows' own unhelpful
  "MoveFile failed; code 3", leaving a half-copied app behind (observed 2026-09-20 with a
  173-character destination). This refuses that destination before a single file is
  copied, and says the number the person has to get under.

  PrepareToInstall is the right hook: it runs in a silent install as well as an
  interactive one, and returning a non-empty string aborts Setup with that message. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  AppDir: String;
  Budget: Integer;
begin
  Result := '';
  AppDir := ExpandConstant('{app}');
  { 259 is Windows' limit, minus one for the separator between the folder and the path
    inside it, minus the deepest path this particular build contains. }
  Budget := 259 - 1 - {#LongestRelPath};
  if Length(AppDir) > Budget then
    Result :=
      'The folder you chose is too long for Windows to handle.' #13#10 #13#10 +
      'Attune contains files whose own names and folders add up to ' +
      '{#LongestRelPath} characters, and Windows refuses any path over 259.' #13#10 #13#10 +
      'You chose a folder ' + IntToStr(Length(AppDir)) + ' characters long:' #13#10 +
      AppDir + #13#10 #13#10 +
      'Please go back and choose one no longer than ' + IntToStr(Budget) +
      ' characters. The suggested folder is well inside the limit.';
end;
