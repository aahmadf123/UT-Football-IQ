@echo off
setlocal
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prod-go.ps1
if errorlevel 1 (
  echo.
  echo Production fix script failed. Review output above.
  pause
  exit /b 1
)
echo.
echo Production deploy and checks passed.
pause
