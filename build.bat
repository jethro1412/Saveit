@echo off
SETLOCAL ENABLEDELAYEDEXPANSION
SET SCRIPT_DIR=%~dp0
CD /D "%SCRIPT_DIR%"

echo ========================================================
echo   Saveit Cross-Platform Executable Builder (Windows)
echo ========================================================

python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [Error] Python 3.9+ is not installed or not in PATH.
    echo Please install Python from https://www.python.org/
    pause
    exit /b 1
)

echo Verifying build dependencies...
pip install -r requirements.txt --quiet --upgrade
pip install pyinstaller --quiet

python build.py %*
IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo [Error] Build process exited with error code %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)

echo.
pause
