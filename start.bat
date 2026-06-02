@echo off
setlocal
cd /d "%~dp0"
set PLAYWRIGHT_BROWSERS_PATH=%~dp0data\ms-playwright
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py
) else (
  python app.py
)
