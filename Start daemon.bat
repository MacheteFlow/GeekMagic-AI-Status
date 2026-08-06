@echo off
rem Starts the daemon that drives the GeekMagic screen.
rem Keep this window open while you work: closing it returns the screen
rem to the stock weather station.
cd /d "%~dp0"
title GeekMagic AI Status
python statusd.py
pause
