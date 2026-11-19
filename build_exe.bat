@echo off
setlocal
cd /d "%~dp0"

set "SCRIPT=relayvsr_batch_gui.py"
set "WORKFLOW=utility_relayvsr_video_x4_upscale.json"
set "APPNAME=RelayVSR_Batch_Upscaler"

if not exist "%SCRIPT%" (
  echo ERROR: %SCRIPT% not found in this folder.
  pause & exit /b 1
)
if not exist "%WORKFLOW%" (
  echo ERROR: %WORKFLOW% not found in this folder.
  pause & exit /b 1
)

rem Find Python: "python" first, then the "py" launcher
set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY (
  py --version >nul 2>&1 && set "PY=py"
)
if not defined PY (
  echo ERROR: Python was not found. Install Python 3 and tick "Add to PATH".
  pause & exit /b 1
)

echo Using: %PY%
%PY% -m pip install --upgrade pyinstaller requests tkinterdnd2
if errorlevel 1 (
  echo ERROR: Could not install the required packages.
  pause & exit /b 1
)

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name %APPNAME% ^
  --collect-all tkinterdnd2 ^
  --add-data "%WORKFLOW%;." ^
  %SCRIPT%
if errorlevel 1 (
  echo ERROR: PyInstaller failed. See the messages above.
  pause & exit /b 1
)

rem Copy the workflow next to the exe too (takes priority over the embedded copy)
copy /y "%WORKFLOW%" "dist\%WORKFLOW%" >nul

echo.
echo Done: dist\%APPNAME%.exe
echo Tip: put ffmpeg.exe next to the exe if ffmpeg is not in your PATH.
pause
