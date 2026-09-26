@echo off
echo Running PC Parts Price Tracker...
cd /d "%~dp0"
C:\Users\jason\AppData\Local\Python\pythoncore-3.14-64\python.exe price_tracker.py %*
if errorlevel 1 goto end
echo.
echo Publishing report to GitHub Pages for phone...
C:\Users\jason\AppData\Local\Python\pythoncore-3.14-64\python.exe publish_report.py
:end
pause
