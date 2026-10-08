# Installation de tout ce dont le pipeline a besoin (Windows 10/11).
# Lancer : clic droit sur install.bat > Exécuter, ou dans PowerShell :
#   powershell -ExecutionPolicy Bypass -File install.ps1            (outils + environnement IA)
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Models    (+ modèles IA, ~8 Go)
#   ... -Blender "D:\Blender\blender.exe"                           (si Blender n'est pas trouvé)

param(
    [switch]$Models,
    [string]$Blender  # chemin de blender.exe si la détection automatique échoue
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Install-Package($id) {
    # winget.exe explicitement : PowerShell ne distingue pas les majuscules,
    # un nom de fonction proche de « winget » se rappellerait lui-même.
    winget.exe install --id $id -e --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {  # -1978335189 = déjà installé
        throw "Échec de l'installation de $id (code $LASTEXITCODE)"
    }
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
    }
    $found = foreach ($d in $dirs) {
        if ($d -and (Test-Path (Join-Path $d "blender.exe"))) { Join-Path $d "blender.exe" }
    }
    return ($found | Sort-Object | Select-Object -Last 1)
}

if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
    throw "winget est introuvable. Installe « App Installer » depuis le Microsoft Store, puis relance."
}

# 1. Logiciels ---------------------------------------------------------------
Step "Git"
if (-not (Have git)) { Install-Package "Git.Git" } else { Write-Host "déjà installé" }

Step "Blender"
if ($Blender -and -not (Test-Path $Blender)) { throw "Blender introuvable : $Blender" }
if (-not $Blender) { $Blender = Find-Blender }
if (-not $Blender) {
    Install-Package "BlenderFoundation.Blender"
    $Blender = Find-Blender
}
if (-not $Blender) {
    throw "Blender introuvable. Relance avec : install.bat -Blender ""C:\chemin\vers\blender.exe"""
}
Set-Content -Path (Join-Path $Root "blender_path.txt") -Value $Blender -Encoding ASCII
Write-Host "Blender : $Blender"

Step "Python 3.11"
Refresh-Path
$py = $null
try { $py = (& py -3.11 -c "import sys; print(sys.executable)") 2>$null } catch {}
if (-not $py) {
    Install-Package "Python.Python.3.11"
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
& $Blender --version | Select-Object -First 1
& $vpy -c "import torch; print('PyTorch', torch.__version__, '| GPU :', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NON DÉTECTÉ')"
& $vpy -c "import diffusers, transformers, mvadapter; print('diffusers', diffusers.__version__, '| transformers', transformers.__version__, '| MV-Adapter OK')"
Write-Host "`nInstallation terminée." -ForegroundColor Green
