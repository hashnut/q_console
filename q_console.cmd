@echo off
chcp 65001 >nul
setlocal
rem q_console launcher.
rem   q_console.cmd                      tray only (no console window)
rem   q_console.cmd --open               tray + dashboard window
rem   q_console.cmd --print              text report in this console
rem   q_console.cmd --install-autostart   start with Windows
set "HERE=%~dp0"
set "PY="
for /f "delims=" %%I in ('where python.exe 2^>nul') do if not defined PY set "PY=%%I"
if not defined PY if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY (
  echo Python 3.10+ 이 필요합니다. https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요.
  pause
  exit /b 1
)
if "%~1"=="" (
  rem No arguments: launch the tray detached and windowless.
  set "PYW=%PY:python.exe=pythonw.exe%"
  start "" "%PY:python.exe=pythonw.exe%" "%HERE%core\__main__.py" --tray
  exit /b 0
)
"%PY%" -X utf8 "%HERE%core\__main__.py" %*
exit /b %errorlevel%
