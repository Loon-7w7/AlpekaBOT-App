; Instalador de AlpekaBOT (Inno Setup 6). Lo compila scripts/release.py con /DAppVersion=x.y.z.
; Los datos del usuario viven en %LOCALAPPDATA%\AlpekaBOT: ni actualizar ni desinstalar los toca.

[Setup]
AppId={{8E3F6B52-4A1D-4C7E-9B20-6D5A1F3C7E94}
AppName=AlpekaBOT
AppVersion={#AppVersion}
AppPublisher=Loon
PrivilegesRequired=lowest
DefaultDirName={autopf}\AlpekaBOT
DisableDirPage=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\release
OutputBaseFilename=AlpekaBOT-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
SetupIconFile=..\alpekabot\assets\app.ico
UninstallDisplayIcon={app}\AlpekaBOT.exe

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"

[InstallDelete]
; Sin esto quedarían archivos sueltos de la versión anterior mezclados con los nuevos
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\AlpekaBOT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\AlpekaBOT"; Filename: "{app}\AlpekaBOT.exe"
Name: "{autodesktop}\AlpekaBOT"; Filename: "{app}\AlpekaBOT.exe"; Tasks: desktopicon

[Run]
; Sin skipifsilent: tras una actualización silenciosa la app se vuelve a abrir sola
Filename: "{app}\AlpekaBOT.exe"; Description: "Abrir AlpekaBOT"; Flags: nowait postinstall
