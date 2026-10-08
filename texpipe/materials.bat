@echo off
setlocal
rem Raccourci Windows pour la composition des materiaux (etape 6).
rem Usage : texpipe\materials.bat mesh_de_jeu_uv.glb [--wear 0.5 --dirt 0.5 --dust 0.2]
rem Produit T_<nom>_BC / _N / _ORM / _E.png et un apercu <nom>_apercu_materiaux.png

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
"%PY%" "%HERE%ai\materials.py" --mesh "%MESH%" %EXTRA%
if errorlevel 1 exit /b 1

rem Apercu (Blender, Cycles sur la carte graphique).
if not defined BLENDER if exist "%HERE%..\blender_path.txt" set /p BLENDER=<"%HERE%..\blender_path.txt"
if not defined BLENDER for /d %%D in ("%ProgramFiles%\Blender Foundation\Blender *") do if exist "%%D\blender.exe" set "BLENDER=%%D\blender.exe"
if not exist "%BLENDER%" (
    echo Blender introuvable : apercu ignore.
    exit /b 0
)
for %%F in ("%MESH%") do set "DIR=%%~dpF" & set "STEM=%%~nF"
if /i "%STEM:~-3%"=="_uv" set "STEM=%STEM:~0,-3%"
"%BLENDER%" -b --factory-startup -P "%HERE%blender\preview_material.py" -- --mesh "%MESH%" --textures-dir "%DIR%." --name "%STEM%" --output "%DIR%%STEM%_apercu_materiaux.png"
