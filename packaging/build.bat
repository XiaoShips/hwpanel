@echo off
rem Rebuild the app and the Inno Setup installer.
setlocal
cd /d "%~dp0\.."

if not exist "packaging\icon.ico" python packaging\make_icon.py || exit /b 1
python -m py_compile hwpanel.py || exit /b 1

python -m PyInstaller --noconfirm --clean --windowed --onedir --name HWPanel ^
  --icon packaging\icon.ico --add-data "tools\lhm;tools\lhm" ^
  --distpath dist --workpath build\pyinstaller hwpanel.py || exit /b 1

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php
  exit /b 1
)

"%ISCC%" packaging\installer.iss || exit /b 1
echo.
echo Built installer in the release folder.
pause
