@echo off
SETLOCAL ENABLEDELAYEDEXPANSION
SET SCRIPT_DIR=%~dp0
CD /D "%SCRIPT_DIR%"

echo ========================================================
echo   Saveit - Telegram Media Saver (Windows GUI Launcher)
echo ========================================================

python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [Error] Python 3.9+ is not installed or not in PATH.
    echo Please download and install Python from https://www.python.org/
    pause
    exit /b 1
)

IF NOT EXIST ".env" (
    IF EXIST ".env.example" (
        copy ".env.example" ".env" >nul
        echo Created initial .env configuration file.
    )
)

echo Checking dependencies...
pip install -r requirements.txt --quiet --upgrade

echo Launching Saveit GUI...
pythonw gui.py >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    python gui.py
)

