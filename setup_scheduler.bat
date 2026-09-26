@echo off
:: PC Parts Price Tracker - Windows Task Scheduler Setup
:: Run this script as Administrator to schedule daily morning reports

SET SCRIPT_DIR=%~dp0
SET PYTHON=C:\Users\jason\AppData\Local\Python\pythoncore-3.14-64\python.exe
SET SCRIPT=%SCRIPT_DIR%price_tracker.py
SET TASK_NAME=PCPartsPriceTracker
SET RUN_TIME=07:00

echo.
echo Setting up PC Parts Price Tracker to run daily at %RUN_TIME%...
echo Script: %SCRIPT%
echo.

:: Check if Python is available
%PYTHON% --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo ERROR: Python not found. Install Python and ensure it is on your PATH.
    pause
    exit /b 1
)

:: Install dependencies
echo Installing Python dependencies...
%PYTHON% -m pip install -r "%SCRIPT_DIR%requirements.txt" --quiet
IF ERRORLEVEL 1 (
    echo WARNING: Could not install dependencies. You may need to run manually:
    echo   pip install -r requirements.txt
)

:: Delete existing task if it exists
SCHTASKS /DELETE /TN "%TASK_NAME%" /F >nul 2>&1

:: Create the scheduled task
SCHTASKS /CREATE ^
  /TN "%TASK_NAME%" ^
  /TR "\"%PYTHON%\" \"%SCRIPT%\"" ^
  /SC DAILY ^
  /ST %RUN_TIME% ^
  /RL HIGHEST ^
  /F

IF ERRORLEVEL 1 (
    echo.
    echo ERROR: Failed to create scheduled task.
    echo Make sure you are running this script as Administrator.
) ELSE (
    echo.
    echo SUCCESS! Task "%TASK_NAME%" created.
    echo The report will run every morning at %RUN_TIME% and open in your browser.
    echo.
    echo To change the time: edit RUN_TIME at the top of this file and re-run.
    echo To run right now:   python price_tracker.py
    echo To remove task:     SCHTASKS /DELETE /TN "%TASK_NAME%" /F
)

echo.
pause
