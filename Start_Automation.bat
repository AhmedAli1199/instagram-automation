@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Instagram Outreach Automation

echo ================================================
echo   Instagram Outreach Automation
echo ================================================
echo.

REM ---------- Find a working Python -- prefer the "py" launcher, more reliable on -----------
REM ---------- Windows than the bare "python" command, which can silently be a     -----------
REM ---------- non-functional Microsoft Store placeholder on some PCs.             -----------
set "PYCMD="
py --version >nul 2>nul
if not errorlevel 1 set "PYCMD=py"
if "%PYCMD%"=="" (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if "%PYCMD%"=="" (
    echo [X] Python is not installed, or isn't set up correctly yet.
    echo.
    echo Please install it from https://www.python.org/downloads/
    echo IMPORTANT: on the very first install screen, tick the box that
    echo says "Add python.exe to PATH" before clicking Install.
    echo Then double-click this file again.
    echo.
    pause
    exit /b 1
)
echo [OK] Python found.

REM ---------- .env file ----------
if not exist ".env" (
    if not exist ".env.example" (
        echo [X] Missing .env.example -- this folder looks incomplete. Contact support.
        pause
        exit /b 1
    )
    copy ".env.example" ".env" >nul
    echo [SETUP] Created your settings file, .env, from the template.
    echo.
    echo Please fill in your Instagram username and password in the Notepad
    echo window that is about to open, then save and close it. After that,
    echo run this file again to continue.
    echo.
    notepad ".env"
    pause
    exit /b 0
)
echo [OK] Settings file found.

REM ---------- Python packages -- checked, not assumed; skips if already present -----------
%PYCMD% -c "import instagrapi, openpyxl, dotenv, langdetect, deep_translator, filelock, tzdata" 2>nul
if errorlevel 1 (
    echo [SETUP] Installing required components for the first time...
    echo         ^(this can take a few minutes and only happens once^)
    %PYCMD% -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo [X] Installation failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
    echo [OK] Components installed.
) else (
    echo [OK] Required components already installed -- skipping.
)

REM ---------- Instagram connection: tries your saved session first, silently. Only  ----------
REM ---------- asks anything, and only then installs the browser login helper, if   ----------
REM ---------- that doesn't work -- see ensure_connection.py.                       ----------
%PYCMD% ensure_connection.py

echo.
echo ================================================
echo   Setup complete. Starting the dashboard...
echo ================================================
echo A browser tab will open automatically in a moment.
echo Keep THIS window open in the background while you want
echo the automation running -- closing it stops everything.
echo.

%PYCMD% dashboard_server.py

echo.
echo The dashboard has stopped.
pause
