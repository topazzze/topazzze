@echo off
rem Double-clic pour mettre a jour le pipeline (remplace les scripts existants,
rem ne touche pas a assets, models, library ni .venv).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
pause
