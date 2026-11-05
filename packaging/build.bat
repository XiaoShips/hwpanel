@echo off
rem Rebuild the app and the Inno Setup installer.
setlocal
cd /d "%~dp0\.."

if not exist "packaging\icon.ico" python packaging\make_icon.py || exit /b 1
python -m py_compile tempeek.py || exit /b 1

python -m PyInstaller --noconfirm --clean --windowed --onedir --name TempPeek ^
  --icon packaging\icon.ico --add-data "tools\lhm;tools\lhm" ^
  --distpath dist --workpath build\pyinstaller tempeek.py || exit /b 1

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php
  exit /b 1
)

"%ISCC%" packaging\installer.iss || exit /b 1

rem 便携版（绿色免安装）：把 dist/TempPeek 的内容直接压到 zip 根，
rem 解压后根目录即 TempPeek.exe，无需进入子文件夹。
python packaging\zip_portable.py || exit /b 1

echo.
echo Built installer and portable zip in the release folder.
pause
