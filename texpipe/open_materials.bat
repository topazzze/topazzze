@echo off
setlocal
rem Materiaux retenus (fichier _materials_choice.txt) : telechargement 2K, scene Blender .blend, ouverture.
rem Usage : texpipe\open_materials.bat mesh_de_jeu_uv.glb [options...]

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
if not defined BLENDER if exist "%HERE%..\blender_path.txt" set /p BLENDER=<"%HERE%..\blender_path.txt"
if not defined BLENDER for /d %%D in ("%ProgramFiles%\Blender Foundation\Blender *") do if exist "%%D\blender.exe" set "BLENDER=%%D\blender.exe"
if not exist "%BLENDER%" (
    echo Blender introuvable : lance install.bat, ou definis BLENDER.
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
set "TEMP=%HERE%..\cache\tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"
for %%F in ("%MESH%") do set "DIR=%%~dpF" & set "STEM=%%~nF"
if /i "%STEM:~-3%"=="_uv" set "STEM=%STEM:~0,-3%"
"%PY%" "%HERE%ai\find_materials.py" --mesh "%MESH%" --final
if errorlevel 1 exit /b 1
"%BLENDER%" -b --factory-startup -P "%HERE%blender\material_scene.py" -- --mesh "%MESH%" --build %EXTRA%
if errorlevel 1 exit /b 1
start "" "%BLENDER%" "%DIR%%STEM%_materials.blend"
