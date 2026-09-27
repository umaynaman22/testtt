; Inno Setup script: turns dist\RentalTracker\ into dist\RentalTracker-Setup-<version>.exe
;   iscc /DAppVersion=0.1.0 packaging\installer.iss
; Installs for the current user (no admin rights needed). Uninstalling never touches your data
; in Documents\RentalTracker.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F3C2B8E-4D1A-4E7B-9C55-2A7E91D4B0C3}
AppName=Rental Tracker
AppVersion={#AppVersion}
AppVerName=Rental Tracker {#AppVersion}
AppPublisher=Rental Tracker
DefaultDirName={autopf}\Rental Tracker
DefaultGroupName=Rental Tracker
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=RentalTracker-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\RentalTracker.exe
UninstallDisplayName=Rental Tracker
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\RentalTracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Rental Tracker"; Filename: "{app}\RentalTracker.exe"
Name: "{group}\Rental Tracker (demo data)"; Filename: "{app}\RentalTracker.exe"; Parameters: "--demo"; Comment: "Try the app with a made-up 60-property portfolio"
Name: "{group}\Uninstall Rental Tracker"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Rental Tracker"; Filename: "{app}\RentalTracker.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RentalTracker.exe"; Description: "{cm:LaunchProgram,Rental Tracker}"; Flags: nowait postinstall skipifsilent
