@echo off
setlocal
cd /d "%~dp0"

rem -- Guard: script must run from the EXTRACTED folder, not from inside the zip
if not exist bot.py (
    echo.
    echo [!] Files not found next to this script.
    echo     Most likely you launched it from inside the ZIP archive.
    echo     Right-click the zip file, choose "Extract All" / "Izvlech vse",
    echo     open the extracted folder and run start_windows.bat again.
    echo.
    pause
    exit /b
)

rem -- First run without .env: create it and ask to fill in the keys
if not exist .env (
    if exist .env.example copy .env.example .env >nul
    echo.
    echo [!] File .env was just created. Notepad will open it now:
    echo     paste your keys, save the file, close Notepad
    echo     and run start_windows.bat again.
    echo.
    start notepad .env
    pause
    exit /b
)

rem -- Find Python
set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 set "PY=python"
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo.
    echo [!] Python not found. Install it from:
    echo     https://www.python.org/downloads/
    echo     IMPORTANT: tick "Add python.exe to PATH" in the installer,
    echo     then run this file again.
    echo.
    pause
    exit /b
)

rem -- Create virtual environment on first run
if not exist .venv (
    echo Preparing environment, this takes about a minute...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [!] Could not create the Python environment. See the error above.
        echo.
        pause
        exit /b
    )
)

call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo [!] Could not install dependencies. Check your internet connection
    echo     and run this file again.
    echo.
    pause
    exit /b
)

rem -- Start the bot (messages below come from the bot itself, in Russian)
python bot.py
echo.
pause
