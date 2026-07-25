@echo off
setlocal
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev-go.ps1 -WithCloudflare -SkipInstall
if errorlevel 1 (
  echo.
  echo Football-IQ startup failed. Check the output above.
  pause
  exit /b 1
)
echo.
echo Football-IQ is ready.
pause
