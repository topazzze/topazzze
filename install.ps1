# Installation de tout ce dont le pipeline a besoin (Windows 10/11).
# Lancer : clic droit sur install.bat > Exécuter, ou dans PowerShell :
#   powershell -ExecutionPolicy Bypass -File install.ps1            (outils + environnement IA)
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Models    (+ modèles IA, ~8 Go)

param([switch]$Models)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Winget($id) {
    winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {  # -1978335189 = déjà installé
        throw "Échec de l'installation de $id (code $LASTEXITCODE)"
    }
}

if (-not (Have winget)) {
    throw "winget est introuvable. Installe « App Installer » depuis le Microsoft Store, puis relance."
}

# 1. Logiciels ---------------------------------------------------------------
Step "Git"
if (-not (Have git)) { Winget "Git.Git" } else { Write-Host "déjà installé" }

Step "Blender"
$blender = Get-ChildItem "$env:ProgramFiles\Blender Foundation\*\blender.exe" -ErrorAction SilentlyContinue |
           Sort-Object FullName | Select-Object -Last 1
if (-not $blender) { Winget "BlenderFoundation.Blender" } else { Write-Host "déjà installé : $($blender.FullName)" }

Step "Python 3.11"
Refresh-Path
$py = $null
try { $py = (& py -3.11 -c "import sys; print(sys.executable)") 2>$null } catch {}
if (-not $py) {
    Winget "Python.Python.3.11"
    Refresh-Path
    $py = (& py -3.11 -c "import sys; print(sys.executable)")
}
Write-Host "Python : $py"

# 2. Environnement Python pour l'IA -------------------------------------------
Step "Environnement Python (.venv)"
$venv = Join-Path $Root ".venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) { & py -3.11 -m venv $venv }
$vpy = "$venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip wheel

Step "PyTorch avec CUDA (carte NVIDIA)"
& $vpy -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128

Step "Bibliothèques du pipeline"
& $vpy -m pip install -r (Join-Path $Root "requirements.txt")

Step "MV-Adapter (code seul, sans nvdiffrast)"
$ext = Join-Path $Root "external"
New-Item -ItemType Directory -Force $ext | Out-Null
if (-not (Test-Path "$ext\MV-Adapter")) {
    git clone --depth 1 https://github.com/huanngzh/MV-Adapter.git "$ext\MV-Adapter"
}
& $vpy -m pip install --no-deps -e "$ext\MV-Adapter"

# 3. Modèles (optionnel) --------------------------------------------------------
if ($Models) {
    Step "Modèles IA (environ 8 Go)"
    $env:HF_HOME = Join-Path $Root "models\huggingface"
    & $vpy -c @"
from huggingface_hub import snapshot_download
for repo, pat in [
    ('huanngzh/mv-adapter', ['mvadapter_ig2mv_sd21.safetensors']),
    ('stabilityai/stable-diffusion-2-1-base', None),
    ('ZhengPeng7/BiRefNet', None),
]:
    print('->', repo)
    snapshot_download(repo, allow_patterns=pat)
"@
}

# 4. Vérification ---------------------------------------------------------------
Step "Vérification"
Refresh-Path
$blender = Get-ChildItem "$env:ProgramFiles\Blender Foundation\*\blender.exe" -ErrorAction SilentlyContinue |
           Sort-Object FullName | Select-Object -Last 1
if ($blender) { & $blender.FullName --version | Select-Object -First 1 } else { Write-Warning "Blender introuvable" }
& $vpy -c "import torch; print('PyTorch', torch.__version__, '| GPU :', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NON DÉTECTÉ')"
& $vpy -c "import diffusers, transformers, mvadapter; print('diffusers', diffusers.__version__, '| transformers', transformers.__version__, '| MV-Adapter OK')"
Write-Host "`nInstallation terminée." -ForegroundColor Green
