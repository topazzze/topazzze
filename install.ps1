# Installation de tout ce dont le pipeline a besoin (Windows 10/11).
# Aucun prérequis : ni winget, ni Git, ni droits administrateur.
# Tout est installé dans le dossier du pipeline (Python, environnement,
# caches, modèles) : rien n'est écrit sur C:.
#
# Lancer : double-clic sur install.bat, ou dans PowerShell :
#   powershell -ExecutionPolicy Bypass -File install.ps1            (environnement IA)
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Models    (+ modèles IA, ~6 Go)
#   ... -Blender "D:\Blender\blender.exe"                           (si Blender n'est pas trouvé)

param(
    [switch]$Models,
    [string]$Blender  # chemin de blender.exe si la détection automatique échoue
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # sinon Invoke-WebRequest est très lent
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = $PSScriptRoot

# Tout reste dans le dossier du pipeline (ex. F:\Pipeline), rien sur C: :
# fichiers temporaires, cache pip, Python, modèles IA.
$Cache = Join-Path $Root "cache"
$Tmp = Join-Path $Cache "tmp"
New-Item -ItemType Directory -Force $Tmp | Out-Null
$env:TEMP = $Tmp
$env:TMP = $Tmp
$env:PIP_CACHE_DIR = Join-Path $Cache "pip"
$ModelsDir = Join-Path $Root "models"
$env:HF_HOME = Join-Path $ModelsDir "huggingface"
$env:TORCH_HOME = Join-Path $ModelsDir "torch"
$PythonDir = Join-Path $Root "tools\python311"

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# Lance un programme et s'arrête s'il échoue (PowerShell ne le fait pas seul).
function Run {
    $exe = $args[0]
    $rest = @($args | Select-Object -Skip 1)
    & $exe @rest
    if ($LASTEXITCODE -ne 0) { throw "Échec ($LASTEXITCODE) : $exe $($rest -join ' ')" }
}

function Download($url, $dest) {
    Write-Host "Téléchargement : $url"
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

function Find-Blender {
    # 1. Chemin mémorisé lors d'une installation précédente
    $saved = Join-Path $Root "blender_path.txt"
    if (Test-Path $saved) {
        $p = (Get-Content $saved -Raw).Trim()
        if (Test-Path $p) { return $p }
    }
    # 2. Dans le PATH
    $cmd = Get-Command blender.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    # 3. Programmes installés (registre), puis emplacements courants (Steam, etc.)
    $keys = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
            "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
            "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    $dirs = @(Get-ItemProperty $keys -ErrorAction SilentlyContinue |
              Where-Object { $_.DisplayName -like "Blender*" -and $_.InstallLocation } |
              ForEach-Object { $_.InstallLocation })
    $dirs += Get-ChildItem "$env:ProgramFiles\Blender Foundation\*" -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
    $dirs += "${env:ProgramFiles(x86)}\Steam\steamapps\common\Blender", "$env:LOCALAPPDATA\Programs\Blender Foundation"
    foreach ($d in Get-PSDrive -PSProvider FileSystem) {
        $dirs += "$($d.Root)SteamLibrary\steamapps\common\Blender"
        $dirs += Get-ChildItem "$($d.Root)Blender*" -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
    }
    $found = foreach ($d in $dirs) {
        if ($d -and (Test-Path (Join-Path $d "blender.exe"))) { Join-Path $d "blender.exe" }
    }
    return ($found | Sort-Object | Select-Object -Last 1)
}

function Find-Python {
    # Python du pipeline (installé dans son dossier), sinon un Python 3.10 à
    # 3.12 déjà présent (il ne prend pas de place en plus : le .venv, lui,
    # est dans le dossier du pipeline).
    $own = Join-Path $PythonDir "python.exe"
    if (Test-Path $own) { return $own }
    foreach ($v in "3.11", "3.12", "3.10") {
        try {
            $p = (& py "-$v" -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $p -and (Test-Path $p)) { return $p }
        } catch {}
    }
    foreach ($v in "311", "312", "310") {
        foreach ($base in "$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles") {
            $p = Join-Path $base "Python$v\python.exe"
            if (Test-Path $p) { return $p }
        }
    }
    return $null
}

# 1. Blender -----------------------------------------------------------------
Step "Blender"
if ($Blender -and -not (Test-Path $Blender)) { throw "Blender introuvable : $Blender" }
if (-not $Blender) { $Blender = Find-Blender }
if (-not $Blender) {
    throw ("Blender introuvable. Installe-le depuis https://www.blender.org/download/ " +
           "ou relance avec : install.bat -Blender ""C:\chemin\vers\blender.exe""")
}
Set-Content -Path (Join-Path $Root "blender_path.txt") -Value $Blender -Encoding ASCII
Write-Host "Blender : $Blender"

# 2. Python ------------------------------------------------------------------
Step "Python"
$py = Find-Python
if (-not $py) {
    $installer = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
    Download "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" $installer
    Write-Host "Installation de Python 3.11 dans $PythonDir ..."
    $p = Start-Process $installer -Wait -PassThru -ArgumentList `
        "/quiet", "InstallAllUsers=0", "TargetDir=""$PythonDir""", "PrependPath=0",
        "Include_launcher=0", "Include_test=0", "Shortcuts=0"
    if ($p.ExitCode -ne 0) { throw "Échec de l'installation de Python (code $($p.ExitCode))" }
    $py = Find-Python
    if (-not $py) { throw "Python installé mais introuvable." }
}
Write-Host "Python : $py"

# 3. Environnement Python pour l'IA --------------------------------------------
Step "Environnement Python (.venv)"
$venv = Join-Path $Root ".venv"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) { Run $py -m venv $venv }
# À chaque lancement de ce Python, les caches des modèles pointent vers le
# dossier du pipeline (Hugging Face, PyTorch), même hors de ce script.
$site = Join-Path $venv "Lib\site-packages"
@"
import os
_root = r"$Root"
os.environ.setdefault("HF_HOME", os.path.join(_root, "models", "huggingface"))
os.environ.setdefault("TORCH_HOME", os.path.join(_root, "models", "torch"))
os.environ.setdefault("PIP_CACHE_DIR", os.path.join(_root, "cache", "pip"))
"@ | Set-Content -Path (Join-Path $site "sitecustomize.py") -Encoding UTF8
Run $vpy -m pip install --upgrade pip wheel

Step "PyTorch avec CUDA (carte NVIDIA, ~3 Go)"
Run $vpy -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128

Step "Bibliothèques du pipeline"
Run $vpy -m pip install -r (Join-Path $Root "requirements.txt")

Step "MV-Adapter (code seul, sans nvdiffrast)"
$ext = Join-Path $Root "external"
$mva = Join-Path $ext "MV-Adapter"
if (-not (Test-Path $mva)) {
    New-Item -ItemType Directory -Force $ext | Out-Null
    $zip = Join-Path $env:TEMP "MV-Adapter.zip"
    Download "https://github.com/huanngzh/MV-Adapter/archive/refs/heads/main.zip" $zip
    Expand-Archive $zip -DestinationPath $ext -Force
    Rename-Item (Join-Path $ext "MV-Adapter-main") "MV-Adapter"
}
Run $vpy -m pip install --no-deps -e $mva

# 4. Modèles (optionnel) ---------------------------------------------------------
if ($Models) {
    Step "Modèles IA (environ 6 Go, dans $ModelsDir)"
    Run $vpy -c @"
from huggingface_hub import snapshot_download
print('-> huanngzh/mv-adapter')
snapshot_download('huanngzh/mv-adapter', allow_patterns=['mvadapter_ig2mv_sd21.safetensors'])
# SD 2.1 a été retiré du compte officiel : copies des mêmes poids en secours.
for repo in ['stabilityai/stable-diffusion-2-1-base', 'Manojb/stable-diffusion-2-1-base']:
    try:
        print('->', repo)
        snapshot_download(repo, allow_patterns=['*.json', '*.txt', '*.safetensors'], ignore_patterns=['*ema*', 'v2-1*'])
        break
    except Exception as e:
        print('   indisponible :', type(e).__name__)
"@
}

# 5. Vérification ------------------------------------------------------------------
Step "Vérification"
& $Blender --version | Select-Object -First 1
Run $vpy -c "import torch; print('PyTorch', torch.__version__, '| GPU :', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NON DETECTE')"
Run $vpy -c "import diffusers, transformers, mvadapter; print('diffusers', diffusers.__version__, '| transformers', transformers.__version__, '| MV-Adapter OK')"
Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "`nInstallation terminée. Tout est dans $Root" -ForegroundColor Green
Write-Host "(Le cache pip dans $Cache peut être supprimé pour gagner ~3 Go.)"
