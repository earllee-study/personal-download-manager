@echo off
REM Build portable onedir bundles + the browser native host into ..\dist
setlocal
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% -c "import PySide6, PyInstaller" >nul 2>nul || (
  echo ERROR: run "%PY% -m pip install pyinstaller pyinstaller-hooks-contrib" first
  exit /b 1
)

%PY% make_icon.py || exit /b 1
%PY% -m PyInstaller --noconfirm --clean --distpath "..\dist" --workpath "..\build" pdm-cli.spec || exit /b 1
%PY% -m PyInstaller --noconfirm --clean --distpath "..\dist" --workpath "..\build" pdm-gui.spec || exit /b 1
%PY% -m PyInstaller --noconfirm --clean --distpath "..\dist" --workpath "..\build" pdm-host.spec || exit /b 1

echo.
echo Built:
echo   dist\pdm-cli\pdm.exe
echo   dist\pdm-gui\pdm-gui.exe
echo   dist\pdm-host\pdm-host.exe
endlocal
