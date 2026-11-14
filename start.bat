@echo off
rem Start stock-news and open the browser. Press Ctrl+C in this window to stop.
cd /d "%~dp0"
python -X utf8 -m stocknews serve --open
if errorlevel 1 pause
