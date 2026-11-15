@echo off
rem ConvertVisioToDrawio - mo giao dien chuyen Visio (.vsd/.vsdx) sang draw.io
setlocal
cd /d "%~dp0"
call "%~dp0app\find_python.bat"
if not defined PY (
  echo.
  echo  Can Python 3.10 tro len de chay ConvertVisioToDrawio.
  echo  Tai Python tai https://www.python.org/downloads/windows/
  echo  Khi cai, tich "Add python.exe to PATH". Cai xong chay lai file nay.
  echo.
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)
set PYTHONDONTWRITEBYTECODE=1
start "ConvertVisioToDrawio" %PYW% "%~dp0app\gui.py"
endlocal
