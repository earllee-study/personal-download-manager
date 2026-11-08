@echo off
setlocal
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
python -m personal_download_manager %*
exit /b %ERRORLEVEL%
