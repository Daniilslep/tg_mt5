@echo off
cd /d "%~dp0"
title SignalKit
color 0A

echo.
echo  ========================================
echo    SignalKit panel
echo  ========================================
echo.
echo  Folder: %CD%
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo  [ERROR] Python not found in PATH.
  echo  Install from https://www.python.org/downloads/
  echo  Check: Add python.exe to PATH
  echo.
  pause
  exit /b 1
)

python --version
echo.

if not exist "panel\server.py" (
  echo  [ERROR] panel\server.py missing
  pause
  exit /b 1
)

if not exist "НАСТРОЙКИ.ini" (
  if exist "НАСТРОЙКИ.пример.ini" copy /Y "НАСТРОЙКИ.пример.ini" "НАСТРОЙКИ.ini" >nul
)

echo  Checking packages...
python -c "import telethon,MetaTrader5,pandas,hcc_reader" 1>nul 2>nul
if errorlevel 1 (
  echo  Installing packages, please wait...
  echo  (Git not required — hcc-reader is bundled in vendor\)
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo  [ERROR] pip install failed
    pause
    exit /b 1
  )
)

echo  Starting engine on http://127.0.0.1:8765/
echo  A browser window should open in 2 seconds.
echo  KEEP THIS WINDOW OPEN.
echo.

REM open browser from bat (more reliable than Python webbrowser)
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8765/"

python panel\server.py
set ERR=%ERRORLEVEL%

echo.
if not "%ERR%"=="0" (
  echo  [ERROR] Engine stopped with code %ERR%
  echo  See details above.
) else (
  echo  Engine stopped.
)
echo.
pause
