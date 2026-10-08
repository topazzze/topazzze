@echo off
setlocal
rem Raccourci Windows pour le decoupage en zones (etape 5).
rem Usage : texpipe\zones.bat mesh_de_jeu_uv.glb [options...]
rem Les images *_Unlit (aplats de couleur) du dossier sont utilisees si presentes.

set "HERE=%~dp0"
set "PY=%HERE%..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Environnement Python introuvable : %PY%
    echo Lance install.bat d'abord.
    exit /b 1
)
if "%~1"=="" (
    echo Usage : %~nx0 mesh_de_jeu_uv.glb [options...]
    exit /b 1
)

set "MESH=%~1"
shift
set "EXTRA="
:collect
if "%~1"=="" goto run
set "EXTRA=%EXTRA% %1"
shift
goto collect

:run
rem Fichiers temporaires sur le disque du pipeline, pas sur C:.
set "TEMP=%HERE%..\cache\tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"
"%PY%" "%HERE%ai\zones.py" --mesh "%MESH%" %EXTRA%
