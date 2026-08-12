@echo off
setlocal
call conda activate phoneprint
if errorlevel 1 (
  echo.
  echo Could not activate the conda environment "phoneprint".
  echo Create it first with:
  echo   conda create -n phoneprint python=3.12 -y
  echo.
  pause
  exit /b 1
)
python server.py
pause
