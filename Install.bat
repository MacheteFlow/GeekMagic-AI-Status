@echo off
rem Double-click this to install. It does everything for you.
cd /d "%~dp0"
title GeekMagic AI Status - Setup

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python does not seem to be installed.
  echo   Get it from https://www.python.org/downloads/
  echo   IMPORTANT: tick "Add Python to PATH" during installation.
  echo.
  pause
  exit /b 1
)

python install.py
