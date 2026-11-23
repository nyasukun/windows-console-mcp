@echo off
setlocal
set "dir=%LOCALAPPDATA%\WindowsConsole"
if not exist "%dir%" mkdir "%dir%"
copy /y "%~dp0bridge.ps1" "%dir%\bridge.ps1" || exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-old.ps1"
set "startup=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WindowsConsole.vbs"
>"%startup%" echo CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File ""%dir%\bridge.ps1""", 0, False
start "" wscript.exe "%startup%"
