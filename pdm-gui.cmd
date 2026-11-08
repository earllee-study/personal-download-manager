@echo off
setlocal
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pythonw -m personal_download_manager.gui
    exit /b 0
)
python -m personal_download_manager.gui
exit /b %ERRORLEVEL%
