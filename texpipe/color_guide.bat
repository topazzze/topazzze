@echo off
setlocal
rem Raccourci Windows pour la couleur guide par IA (etape 4).
rem Usage : texpipe\color_guide.bat mesh_de_jeu_uv.glb reference_face.png [options...]
rem Exemple : texpipe\color_guide.bat robot_uv.glb robot_front.png --prompt "dark metal robot"

set "HERE=%~dp0"
set "PY=%HERE%..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Environnement Python introuvable : %PY%
    echo Lance install.bat d'abord.
    exit /b 1
)
if "%~2"=="" (
    echo Usage : %~nx0 mesh_de_jeu_uv.glb reference_face.png [options...]
    exit /b 1
)

set "MESH=%~1"
set "IMG=%~2"
shift
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
"%PY%" "%HERE%ai\color_guide.py" --mesh "%MESH%" --image "%IMG%" %EXTRA%
