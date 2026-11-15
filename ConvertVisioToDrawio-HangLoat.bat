@echo off
rem Chuyen hang loat: moi file .vsd/.vsdx trong input\ -> drawio_files\
rem Them --strict de chi xuat file PASS (bo qua file REVIEW).
setlocal
cd /d "%~dp0"
chcp 65001 >nul
call "%~dp0app\find_python.bat"
if not defined PY (
  echo.
  echo  Can Python 3.10 tro len. Tai tai https://www.python.org/downloads/windows/
  echo.
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)
set PYTHONDONTWRITEBYTECODE=1
set PYTHONIOENCODING=utf-8
%PY% "%~dp0app\run_cli.py" %*
echo.
echo Ket qua: %~dp0drawio_files    Chi tiet tung file: %~dp0work
pause
endlocal
