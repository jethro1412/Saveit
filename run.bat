@echo off
SETLOCAL ENABLEDELAYEDEXPANSION
SET SCRIPT_DIR=%~dp0
CD /D "%SCRIPT_DIR%"

echo ========================================================
echo   Saveit - Telegram Media Saver & Tutorial Forwarder
echo ========================================================
echo.

python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [Error] Python is not installed. Please install Python 3.9+ first.
    pause
    exit /b 1
)

pip --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [Error] pip is not installed. Please install pip.
    pause
    exit /b 1
)

echo Installing / verifying dependencies...
pip install -r requirements.txt --quiet --upgrade

echo.
echo Select Mode to Launch:
echo  [1] Windows GUI Application (Recommended)
echo  [2] CLI Console Mode
echo  [3] Build Standalone Executable (GUI, CLI, or Custom Branding)
echo.
set /p MODE_CHOICE="Enter selection [1, 2, or 3] (default 1): "

IF "%MODE_CHOICE%"=="2" (
    IF NOT EXIST ".env" (
        copy ".env.example" ".env" >nul
        set /p API_ID="Enter your API_ID: "
        set /p API_HASH="Enter your API_HASH: "
        set /p HANDLER="Enter your HANDLER (e.g., .saveit): "
        set /p FORWARD_GROUP_IDS="Enter FORWARD_GROUP_IDS (optional, e.g. -1001234567890,@learnpython): "
        powershell -Command "(gc .env) -replace 'API_ID=.*','API_ID=%API_ID%' | Out-File -encoding ASCII .env"
        powershell -Command "(gc .env) -replace 'API_HASH=.*','API_HASH=%API_HASH%' | Out-File -encoding ASCII .env"
        powershell -Command "(gc .env) -replace 'HANDLER=.*','HANDLER=%HANDLER%' | Out-File -encoding ASCII .env"
        powershell -Command "(gc .env) -replace 'FORWARD_GROUP_IDS=.*','FORWARD_GROUP_IDS=%FORWARD_GROUP_IDS%' | Out-File -encoding ASCII .env"
    )
    python Saveit.py
) ELSE IF "%MODE_CHOICE%"=="3" (
    call build.bat --interactive
) ELSE (
    echo Launching Windows GUI...
    pythonw gui.py >nul 2>&1
    IF %ERRORLEVEL% NEQ 0 (
        python gui.py
    )
)

echo.
pause
