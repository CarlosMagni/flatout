@echo off
rem Double-click: installs (or updates) RaceLab and creates the shortcuts.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 install.py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python install.py
  ) else (
    echo Python 3.11 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
    echo then double-click "Install RaceLab.bat" again.
  )
)
echo.
pause
