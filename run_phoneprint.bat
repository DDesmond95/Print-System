@echo off
setlocal
call conda activate phoneprint
if errorlevel 1 (
  echo The conda environment "phoneprint" does not exist.
  echo.
  echo Create it with:
  echo   conda create -n phoneprint python=3.12 -y
  echo   conda activate phoneprint
  echo   pip install -r requirements.txt
  pause
  exit /b 1
)
python server.py
pause
