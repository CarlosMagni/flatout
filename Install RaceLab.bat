@echo off
rem Double-click: installs (or updates) RaceLab and creates the shortcuts.
cd /d "%~dp0"
rem "if errorlevel" is read when the line runs (a %errorlevel% inside a block is read before it)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 install.py
  goto done
)
where python >nul 2>nul
if not errorlevel 1 (
  python install.py
  goto done
)
echo Python 3.11 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then double-click "Install RaceLab.bat" again.
:done
echo.
pause
