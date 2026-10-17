; ktrade Windows 설치 프로그램 (Inno Setup 6.4+ / 7)
;
;   ISCC.exe /DAppVersion=0.4.3 installer\ktrade-setup.iss      → dist\ktrade-setup.exe
;   (릴리스마다 .github/workflows/installer.yml 이 빌드해 릴리스에 첨부)
;
; 설치 프로그램 자체는 작습니다. 실행하면 scripts\install.ps1을 돌려
; uv → 최신 릴리스의 ktrade(자체 파이썬 3.12 환경) → ktrade init → 바로가기 순으로 설치합니다.
; 관리자 권한이 필요 없고, 설치 중 인터넷 연결이 필요합니다.
; 무인 설치: ktrade-setup.exe /VERYSILENT [/TASKS="!dashboard,!shortcut"]

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7B0E3C52-6A41-4F0B-9C7E-2D5A8E61B4F3}
AppName=ktrade
AppVersion={#AppVersion}
AppVerName=ktrade {#AppVersion}
AppPublisher=Re-qwer3618
AppPublisherURL=https://github.com/Re-qwer3618/ktrade-public
AppSupportURL=https://github.com/Re-qwer3618/ktrade-public/issues
DefaultDirName={localappdata}\ktrade-setup
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ktrade-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
InfoBeforeFile=..\DISCLAIMER.md
UninstallDisplayName=ktrade
MinVersion=10.0

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Messages]
korean.WelcomeLabel2=국내주식 매매조건 실험 엔진 ktrade {#AppVersion} 을(를) 설치합니다.%n%n설치 중 인터넷에서 uv(파이썬 도구 관리자)와 ktrade 최신 릴리스를 받습니다. 관리자 권한이나 파이썬을 미리 설치할 필요가 없습니다.
korean.FinishedHeadingLabel=ktrade 설치를 마쳤습니다
korean.FinishedLabelNoIcons=바탕화면의 'ktrade 대시보드'를 더블클릭하면 브라우저에 화면이 열립니다.%n%n· 키움 앱키가 있으면: 로그인 화면에 키를 넣고 시세 받기%n· 키가 없으면: '체험 모드'로 가상 데이터로 먼저 둘러보기%n%n자세한 사용법은 설치 가이드를 참고하세요.

[Tasks]
Name: "dashboard"; Description: "대시보드 구성 요소 설치 (streamlit, 권장)"
Name: "shortcut"; Description: "바탕화면·시작 메뉴 바로가기 만들기 (대시보드·명령창·폴더)"

[Files]
Source: "..\scripts\install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "run-install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "uninstall.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{%USERPROFILE}\.local\bin\ktrade.exe"; Parameters: "dashboard --open"; Description: "ktrade 대시보드 열기 (로그인 또는 체험 모드)"; Flags: postinstall nowait skipifsilent; Check: InstallSucceeded
Filename: "https://github.com/Re-qwer3618/ktrade-public/blob/main/docs/INSTALL.md"; Description: "설치 가이드 보기"; Flags: postinstall shellexec nowait skipifsilent unchecked

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\uninstall.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveKtrade"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  Installed: Boolean;

function InstallSucceeded: Boolean;
begin
  Result := Installed;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Params: String;
  Code: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\run-install.ps1') + '"';
  if not WizardIsTaskSelected('dashboard') then
    Params := Params + ' -NoDashboard';
  if not WizardIsTaskSelected('shortcut') then
    Params := Params + ' -NoShortcut';
  if WizardSilent then
    Params := Params + ' -Silent';
  WizardForm.StatusLabel.Caption := 'ktrade 내려받아 설치하는 중... (몇 분 걸릴 수 있습니다)';
  Installed := Exec('powershell.exe', Params, ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, Code) and (Code = 0);
  if not Installed then
    SuppressibleMsgBox('ktrade 설치를 끝내지 못했습니다.' + #13#10#13#10 +
      '인터넷 연결을 확인하고 설치 프로그램을 다시 실행해 주세요.' + #13#10 +
      '로그: ' + ExpandConstant('{localappdata}\ktrade-setup\install.log'),
      mbError, MB_OK, IDOK);
end;
