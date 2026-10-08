@echo off
rem Double-clic pour tout installer. Ajouter -Models pour télécharger aussi les modèles IA.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
