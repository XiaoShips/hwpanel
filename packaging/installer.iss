; =====================================================================
;  TempPeek 安装脚本（Inno Setup 6）
;  构建：ISCC.exe packaging\installer.iss
; =====================================================================
#define AppName "TempPeek"
#define AppVersion "2.2.0"
#define AppPublisher "TempPeek"
#define AppExe "TempPeek.exe"

[Setup]
AppId={{8C4F2A1E-7D3B-4E62-9F5A-3B1C0D9E8A21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppComments=Windows 桌面硬件监控悬浮面板
DefaultDirName={autopf}\TempPeek
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
AppMutex=TempPeek_SingleInstance_v1
OutputDir=..\release
OutputBaseFilename=TempPeek-Setup-v2.2.0
SetupIconFile=icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
ShowLanguageDialog=no
VersionInfoVersion=2.2.0.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: checkedonce
Name: "driver"; Description: "安装 PawnIO 传感器驱动（CPU 温度 / 风扇转速，可卸载）"; GroupDescription: "传感器驱动："; Flags: checkedonce

[Files]
Source: "..\dist\TempPeek\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\TempPeek\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "第三方组件说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "redist\PawnIO_setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Tasks: driver; Check: WantDriver

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{group}\卸载{#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\PawnIO_setup.exe"; Parameters: "-install -silent"; StatusMsg: "正在安装 PawnIO 传感器驱动…"; Flags: runhidden waituntilterminated; Check: WantDriver
Filename: "{app}\{#AppExe}"; Description: "启动{#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function PawnIOInstalled(): Boolean;
var
  S: string;
begin
  Result := RegQueryStringValue(HKLM64, 'SYSTEM\CurrentControlSet\Services\PawnIO', 'ImagePath', S)
         or RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Services\PawnIO', 'ImagePath', S);
end;

function WantDriver(): Boolean;
begin
  Result := WizardIsTaskSelected('driver') and not PawnIOInstalled();
end;
