@echo off
rem Raccourci Windows pour l'optimisation UV.
rem Usage : texpipe\uv_optimize.bat entree.glb sortie.glb [options...]
rem Exemple : texpipe\uv_optimize.bat robot_low.glb robot_low_uv.glb --texture-size 4096 --preview apercu.png

rem Blender est détecté automatiquement (version la plus récente installée).
rem Pour forcer un chemin : set "BLENDER=C:\...\blender.exe" avant de lancer.
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
"%BLENDER%" -b --factory-startup -P "%~dp0blender\uv_optimize.py" -- --input "%IN%" --output "%OUT%" %EXTRA%
