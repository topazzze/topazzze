@echo off
setlocal
rem Raccourci Windows pour l'optimisation UV.
rem Usage : texpipe\uv_optimize.bat entree.glb sortie.glb [options...]
rem Exemple : texpipe\uv_optimize.bat robot_low.glb robot_low_uv.glb --texture-size 4096 --preview apercu.png

rem Dossier du script, mémorisé AVANT les "shift" (qui décalent aussi %0).
set "HERE=%~dp0"

rem Blender est détecté automatiquement (version la plus récente installée).
rem Pour forcer un chemin : set "BLENDER=C:\...\blender.exe" avant de lancer.
if not defined BLENDER if exist "%HERE%..\blender_path.txt" (
    set /p BLENDER=<"%HERE%..\blender_path.txt"
)
if not defined BLENDER (
    for /d %%D in ("%ProgramFiles%\Blender Foundation\Blender *") do (
        if exist "%%D\blender.exe" set "BLENDER=%%D\blender.exe"
    )
)

if not exist "%BLENDER%" (
    echo Blender introuvable : %BLENDER%
    echo Lance install.bat, ou definis BLENDER avec le chemin de blender.exe
    exit /b 1
)
if "%~2"=="" (
    echo Usage : %~nx0 entree.glb sortie.glb [options...]
    exit /b 1
)

set "IN=%~1"
set "OUT=%~2"
shift
shift
set "EXTRA="
:collect
if "%~1"=="" goto run
set "EXTRA=%EXTRA% %1"
shift
goto collect

:run
rem Fichiers temporaires de Blender (tuiles de rendu...) sur le disque du
rem pipeline, pas dans C:\Users\...\Temp.
set "TEMP=%HERE%..\cache\tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"
"%BLENDER%" -b --factory-startup -P "%HERE%blender\uv_optimize.py" -- --input "%IN%" --output "%OUT%" %EXTRA%
