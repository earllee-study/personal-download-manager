; Inno Setup script for Personal Download Manager (PDM).
; Build the portable bundles first:  packaging\build.cmd
; Then compile:                      ISCC.exe packaging\pdm.iss
; Output:                            dist\pdm-setup-<version>.exe
; CI passes the version:             ISCC.exe /DAppVersion=0.2.0 packaging\pdm.iss

#define AppName "Personal Download Manager"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppPublisher "Personal Download Manager project"
#define AppExeName "pdm-gui.exe"
#define SourceRoot "..\dist"

[Setup]
AppId={{AE6E9C04-B0F5-47DF-8877-26BC241BD3C7}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Personal Download Manager
DefaultGroupName=Personal Download Manager
DisableProgramGroupPage=yes
OutputDir={#SourceRoot}
OutputBaseFilename=pdm-setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\gui\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
MinVersion=10.0

[Languages]
; The Simplified Chinese translation is not bundled with Inno Setup; drop a
; ChineseSimplified.isl into its Languages folder and switch this line if
; you want a localized wizard.
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked
Name: "browserintegration"; Description: "注册浏览器集成（Chrome/Edge；扩展仍需在浏览器里手动加载）"; GroupDescription: "附加任务:"

[Files]
Source: "{#SourceRoot}\pdm-gui\*"; DestDir: "{app}\gui"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#SourceRoot}\pdm-cli\*"; DestDir: "{app}\cli"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#SourceRoot}\pdm-host\*"; DestDir: "{app}\host"; Flags: recursesubdirs createallsubdirs ignoreversion; Tasks: browserintegration
Source: "..\browser\extension\*"; DestDir: "{app}\browser-extension"; Flags: recursesubdirs createallsubdirs ignoreversion; Tasks: browserintegration
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
; Public documentation only.  Do NOT use a docs\* wildcard here: the working
; tree can contain gitignored local notes (handoff, development status,
; internal analysis, SSH setup) that must never be shipped in a public
; installer.
Source: "..\docs\browser-integration.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\browser-verification-2026-11-01.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\browser-verification-2026-11-02.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\browser-verification-2026-11-04-mse-multitrack.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\browser-verification-2026-11-04-mse-real-site.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\browser-verification-2026-11-04-mse-reset-stop.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\browser-verification-2026-11-07-mse-chrome.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\browser-verification-2026-11-07-mse-queue.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\browser-verification-2026-11-08-mse-webm.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\ci.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\firefox-mse-mainworld-evaluation-2026-11-08.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\gui.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\gui.png"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\mse-site-matrix-2026-11-08.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\testing.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\updating.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\signing.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Registry]
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.pdm.downloader.host"; ValueType: string; ValueData: "{app}\com.pdm.downloader.host.json"; Flags: uninsdeletekey; Tasks: browserintegration
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.pdm.downloader.host"; ValueType: string; ValueData: "{app}\com.pdm.downloader.host.json"; Flags: uninsdeletekey; Tasks: browserintegration

[Icons]
Name: "{group}\Personal Download Manager"; Filename: "{app}\gui\{#AppExeName}"
Name: "{group}\Personal Download Manager 命令行"; Filename: "{app}\cli\pdm.exe"
Name: "{group}\卸载 Personal Download Manager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Personal Download Manager"; Filename: "{app}\gui\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\gui\{#AppExeName}"; Description: "启动 Personal Download Manager"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\gui"
Type: filesandordirs; Name: "{app}\cli"
Type: files; Name: "{app}\com.pdm.downloader.host.json"

[Code]
const
  ExtensionId = 'hfiihggbmdmoofcadgmkingenjgcaend';

procedure WriteNativeManifest();
var
  Lines: TArrayOfString;
  HostPath: String;
begin
  HostPath := ExpandConstant('{app}\host\pdm-host.exe');
  StringChangeEx(HostPath, '\', '\\', True);
  SetArrayLength(Lines, 7);
  Lines[0] := '{';
  Lines[1] := '  "name": "com.pdm.downloader.host",';
  Lines[2] := '  "description": "Personal Download Manager native host",';
  Lines[3] := '  "path": "' + HostPath + '",';
  Lines[4] := '  "type": "stdio",';
  Lines[5] := '  "allowed_origins": ["chrome-extension://' + ExtensionId + '/"]';
  Lines[6] := '}';
  SaveStringsToUTF8FileWithoutBOM(ExpandConstant('{app}\com.pdm.downloader.host.json'), Lines, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('browserintegration') then
    WriteNativeManifest();
end;
