#define MyAppName      "Aperio"
#define MyAppPublisher "Wawtor"
#define MyAppURL       "https://github.com/wawtor/aperio"
#define MyAppExe       "aperio.exe"
#define MyAppSetupExe  "aperio_setup.exe"

[Setup]
AppId={{2E4A8F3C-7B91-4D5E-A6C2-1B8D3F5E9A7C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=Aperio-v{#MyAppVersion}-Setup
SetupIconFile=..\aperio.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ChangesEnvironment=yes
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\daemon\target\release\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\{#MyAppSetupExe}";             DestDir: "{app}"; Flags: ignoreversion
Source: "..\aperio.ico";                         DestDir: "{app}"; Flags: ignoreversion
; `aperio` command shim -- {app}\bin goes on PATH so it can be run from any terminal
Source: "aperio.cmd";                            DestDir: "{app}\bin"; Flags: ignoreversion

[Registry]
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
  ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\bin"; \
  Check: NeedsAddPath(ExpandConstant('{app}\bin'))

[Icons]
Name: "{group}\Aperio Setup"; Filename: "{app}\{#MyAppSetupExe}"; IconFilename: "{app}\aperio.ico"
Name: "{group}\Uninstall Aperio"; Filename: "{uninstallexe}"

[Run]
; Register the daemon as a logon task
Filename: "{sys}\schtasks.exe"; \
  Parameters: "/Create /TN ""Aperio"" /TR """"""{app}\aperio.exe"""""" /SC ONLOGON /RL LIMITED /F"; \
  Flags: runhidden waituntilterminated; \
  StatusMsg: "Registering startup task..."
; Start it immediately without waiting for next logon
Filename: "{sys}\schtasks.exe"; \
  Parameters: "/Run /TN ""Aperio"""; \
  Flags: runhidden nowait; \
  StatusMsg: "Starting Aperio..."
; Offer to launch the setup GUI
Filename: "{app}\{#MyAppSetupExe}"; \
  Description: "Configure startup position (recommended)"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM aperio.exe /F";  Flags: runhidden; RunOnceId: "Kill"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""Aperio"" /F"; Flags: runhidden; RunOnceId: "Task"

[Code]
const
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Path, BinDir: string;
  P: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    BinDir := ExpandConstant('{app}\bin');
    if RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', Path) then
    begin
      P := Pos(';' + Uppercase(BinDir) + ';', ';' + Uppercase(Path) + ';');
      if P > 1 then
        Delete(Path, P - 1, Length(BinDir) + 1)
      else if P = 1 then
        Delete(Path, 1, Length(BinDir) + 1);
      if P > 0 then
        RegWriteExpandStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', Path);
    end;
  end;
end;
