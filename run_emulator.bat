@echo off
rem Starts the P10R / P10R+ emulator window.  Extra arguments are passed
rem through, e.g.  run_emulator.bat --cpu p10r   or   run_emulator.bat --cpu ra --band L8
cd /d "%~dp0"
python -m p10r_emu %*
if errorlevel 1 pause
