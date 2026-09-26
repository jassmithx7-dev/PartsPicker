@echo off
REM Morning scrape + publish (for Task Scheduler). No pause.
cd /d "%~dp0"
C:\Users\jason\AppData\Local\Python\pythoncore-3.14-64\python.exe price_tracker.py --publish
