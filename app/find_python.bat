@echo off
rem Tim Python 3.10+ tren may. Tra ve bien PY (chay co cua so) va PYW (chay khong cua so).
set "PY="
set "PYW="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PY=py -3"
    set "PYW=pyw -3"
    goto :eof
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PY=python"
    set "PYW=pythonw"
    goto :eof
  )
)
goto :eof
