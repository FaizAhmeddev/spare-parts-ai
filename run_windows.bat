@echo off
REM SpareAI one-click setup + run for Windows
REM Change PORT below if 8010 is also taken (8000 is left free for your other apps)
set PORT=8010
if not "%SPAREAI_PORT%"=="" set PORT=%SPAREAI_PORT%
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
echo Installing dependencies (first time only takes a minute)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e .
echo.
echo === Validating data ===
python -m spareai validate
echo.
echo === Running analysis ===
python -m spareai run || (echo Analysis failed - see outputs\spareai.log & pause & exit /b 1)
echo.
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul && (
  echo Port %PORT% is already used by another application.
  echo Edit PORT at the top of run_windows.bat, or run: set SPAREAI_PORT=8020
  pause & exit /b 1
)
echo === Starting dashboard on http://localhost:%PORT%  (Ctrl+C to stop) ===
start "" http://localhost:%PORT%
python -m spareai serve --port %PORT%
