@echo off
echo Starting PC Parts Price Tracker companion server...
echo Open http://localhost:5001 in your browser.
echo Press Ctrl+C to stop.
echo.
cd /d "%~dp0"
C:\Users\jason\AppData\Local\Python\pythoncore-3.14-64\python.exe server.py
pause
