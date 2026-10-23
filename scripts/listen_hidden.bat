@echo off
rem Silent start (no window): used by the auto-start shortcut in the Startup folder.
cd /d "%~dp0.."
"%LocalAppData%\hermes\hermes-agent\venv\Scripts\pythonw.exe" -u -m main listen >> state\listen.log 2>&1
