@echo off
rem Raccourci Windows pour le calcul des cartes (etape 3).
rem Usage : texpipe\bake_maps.bat mesh_de_jeu_uv.glb highpoly.glb [options...]
rem Exemple : texpipe\bake_maps.bat robot_uv.glb robot_high.glb --texture-size 4096 --preview apercu_bake.png

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
    echo Usage : %~nx0 mesh_de_jeu_uv.glb highpoly.glb [options...]
    exit /b 1
)

set "LOW=%~1"
set "HIGH=%~2"
shift
shift
set "EXTRA="
:collect
if "%~1"=="" goto run
set "EXTRA=%EXTRA% %1"
shift
goto collect

:run
"%BLENDER%" -b --factory-startup -P "%HERE%blender\bake_maps.py" -- --low "%LOW%" --high "%HIGH%" %EXTRA%
