; Compile via build_installer.py. Payload is the complete directory distribution.
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef OutputPath
  #error OutputPath is required
#endif
[Setup]
AppId={{0C34735C-E7DC-4C6E-A68D-622BCDF8E761}
AppName=PaperTranslator
AppVersion={#AppVersion}
AppVerName=PaperTranslator {#AppVersion}
AppPublisher=foogtil
AppPublisherURL=https://github.com/foogtil/PaperTranslator
AppSupportURL=https://github.com/foogtil/PaperTranslator/issues
AppUpdatesURL=https://github.com/foogtil/PaperTranslator/releases
DefaultDirName={localappdata}\Programs\PaperTranslator
DefaultGroupName=PaperTranslator
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
DisableProgramGroupPage=yes
DisableDirPage=no
LicenseFile=..\LICENSE
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\PaperTranslator.exe
OutputDir={#OutputPath}
OutputBaseFilename=PaperTranslator-{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName=PaperTranslator
VersionInfoDescription=PaperTranslator Setup
UninstallDisplayName=PaperTranslator {#AppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PaperTranslator"; Filename: "{app}\PaperTranslator.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\PaperTranslator"; Filename: "{app}\PaperTranslator.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\{cm:UninstallProgram,PaperTranslator}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\PaperTranslator.exe"; Description: "{cm:LaunchProgram,PaperTranslator}"; Flags: nowait postinstall skipifsilent

; No UninstallDelete: retain settings, keys, jobs and user-created documents.
