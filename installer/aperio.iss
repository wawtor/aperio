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
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\daemon\target\release\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\{#MyAppSetupExe}";             DestDir: "{app}"; Flags: ignoreversion
Source: "..\aperio.ico";                         DestDir: "{app}"; Flags: ignoreversion

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
