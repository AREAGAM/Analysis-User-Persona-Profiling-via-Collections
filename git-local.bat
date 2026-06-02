@echo off
setlocal
cd /d "%~dp0"
set PATH=%~dp0tools\mingit\mingw64\bin;%~dp0tools\mingit\usr\bin;%PATH%
"%~dp0tools\mingit\cmd\git.exe" %*
