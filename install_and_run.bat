@echo off
title UniversalSubs Installer
color 0A
echo ============================================
echo   UniversalSubs - Live Translated Captions
echo ============================================
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found! Install from https://python.org
    echo IMPORTANT: Check "Add Python to PATH" during install!
    pause & exit /b 1
)

python -c "import universalsubs" 2>nul
if not errorlevel 1 (
    echo Already installed - launching.
    goto :proctap_step
)
echo [1/3] Installing UniversalSubs (this folder, editable)...
python -m pip install -e . 
if errorlevel 1 ( echo [ERROR] Install failed - see above. & pause & exit /b 1 )

:proctap_step
python -c "import proctap" 2>nul
if not errorlevel 1 ( echo [2/3] Per-app capture already installed. & goto :launch )
echo [2/3] OPTIONAL per-app capture (Windows 11)...
if exist wheels\ (
    python -m pip install --no-index --find-links wheels proc-tap --quiet 2>nul
)
python -c "import proctap" 2>nul
if not errorlevel 1 ( echo   Per-app capture OK ^(bundled wheel^). & goto :launch )
echo   (skipped - app works with "All system audio")

:launch
echo [3/3] Launching UniversalSubs...
python -m universalsubs
if errorlevel 1 ( echo. & echo Error - see message above. & pause )
