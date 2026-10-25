@echo off
setlocal
cd /d "%~dp0..\.."
set "LABELER_CATALOG=reports\overhaul\labeling-package\sources-content-verified.json"
if not exist "%LABELER_CATALOG%" set "LABELER_CATALOG=reports\overhaul\labeling-package\sources.json"
python tools\football-labeler\server.py --catalog "%LABELER_CATALOG%" --drafts runs\staff-review-20261021 --proposals runs\practice-detection-review-20261021 --library practice --port 8042 --open
if errorlevel 1 (
  echo.
  echo Could not start the local viewer. Install Python 3.11+ and check README.md.
  echo If port 8042 is already in use, open http://127.0.0.1:8042 or choose another port in README.md.
  pause
)
