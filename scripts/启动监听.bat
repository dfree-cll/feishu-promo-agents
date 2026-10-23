@echo off
rem Double-click to start the listener with a visible log window.
cd /d "%~dp0.."
"%LocalAppData%\hermes\hermes-agent\venv\Scripts\python.exe" -u -m main listen
pause
