@echo off
SETLOCAL ENABLEDELAYEDEXPANSION
SET SCRIPT_DIR=%~dp0
CD /D "%SCRIPT_DIR%"

echo ========================================================
echo   Saveit - Standalone Windows Executable (.exe) Builder
echo ========================================================

python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [Error] Python 3.9+ is not installed or not in PATH.
    echo Please install Python from https://www.python.org/
    pause
    exit /b 1
)

echo [1/3] Installing build dependencies...
pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

echo.
echo [2/3] Building standalone Saveit.exe via PyInstaller...
pyinstaller --clean -y Saveit.spec

IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo [Error] Build failed! Check the output above.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [3/3] Build completed successfully!
echo Executable generated at: dist\Saveit.exe
echo.
pause
