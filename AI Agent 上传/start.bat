@echo off
cd /d "%~dp0"

rem ===== If service already running: open browser and exit =====
netstat -ano | findstr ":8234 " | findstr "LISTENING" >nul 2>nul
if %errorlevel%==0 (
  start "" http://127.0.0.1:8234
  exit /b 0
)

rem ===== Start server in background (pythonw = no console window) =====
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw server.py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    start "" python server.py
  ) else (
    echo [ERROR] Python not found. Please install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
  )
)

rem ===== Wait for port 8234, then open browser (max 15s) =====
set N=0
:wait
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8234 " | findstr "LISTENING" >nul 2>nul
if %errorlevel%==0 goto open
set /a N+=1
if %N% lss 15 goto wait

:open
start "" http://127.0.0.1:8234
exit /b 0
