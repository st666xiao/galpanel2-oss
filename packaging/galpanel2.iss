; Compile after PyInstaller: ISCC.exe /DAppVersion=2.1.0 packaging\galpanel2.iss
#ifndef AppVersion
  #error AppVersion must be supplied with /DAppVersion=<version>
#endif

[Setup]
AppId={{7A9C5541-CE57-4F9A-AB35-A588DDB11327}
AppName=GalPanel2
AppVersion={#AppVersion}
AppPublisher=GalPanel2 contributors
AppPublisherURL=https://github.com/st666xiao/galpanel2-oss
DefaultDirName={localappdata}\Programs\GalPanel2
DefaultGroupName=GalPanel2
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=GalPanel2-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\GalPanel2.exe
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; Only program files: never register user libraries/settings for uninstall.
Source: "..\dist\GalPanel2\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\GalPanel2"; Filename: "{app}\GalPanel2.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\GalPanel2"; Filename: "{app}\GalPanel2.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\GalPanel2.exe"; Description: "Launch GalPanel2"; Flags: nowait postinstall skipifsilent
