@echo off
title UniversalSubs
python -m universalsubs
if errorlevel 1 (
    echo Error - if UniversalSubs is not installed, run install_and_run.bat first.
    pause
)
