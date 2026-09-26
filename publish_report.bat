@echo off
REM Publish morning_report.html to GitHub Pages for phone viewing.
cd /d "%~dp0"
C:\Users\jason\AppData\Local\Python\pythoncore-3.14-64\python.exe publish_report.py
pause
