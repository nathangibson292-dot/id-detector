@echo off
REM Double-click launcher: start the local IDea web app and open the browser.
REM Runs the 127.0.0.1-only server from this repo folder; close this window (or Ctrl-C) to stop.
cd /d "%~dp0"
uv run idea serve --open
if errorlevel 1 pause
