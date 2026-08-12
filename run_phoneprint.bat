@echo off
setlocal
call conda activate phoneprint
if errorlevel 1 (
  echo Could not activate conda environment "phoneprint".
  echo.
  echo Create it first:
  echo   conda create -n phoneprint python=3.12 -y
  echo   conda activate phoneprint
  echo   pip install -r requirements.txt
  pause
  exit /b 1
)
python server.py
pause
