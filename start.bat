@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Please install Python 3 and check "Add python.exe to PATH".
    pause
    exit /b 1
)
python reader.py %*
if errorlevel 1 pause
