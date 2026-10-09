# Mise à jour du pipeline depuis GitHub (branche claude/keen-heisenberg-we6ihj).
# Les fichiers du pipeline (scripts, documentation, installation) sont
# REMPLACÉS même s'ils existent ou ont été modifiés ; le dossier assets, les
# modèles, la bibliothèque de matériaux et l'environnement Python ne sont pas
# touchés.
#
# Lancer : double-clic sur update.bat

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = $PSScriptRoot
$Branch = "claude/keen-heisenberg-we6ihj"
$Repo = "topazzze/topazzze"
# Chemins remplacés à chaque mise à jour (jamais assets\, models\, library\, .venv\).
$Managed = @("texpipe", "tests", "install.ps1", "install.bat", "update.ps1", "update.bat",
             "requirements.txt", "WORKFLOW.txt", "README.md", ".gitattributes", ".gitignore")

$Tmp = Join-Path $Root "cache\tmp"
New-Item -ItemType Directory -Force $Tmp | Out-Null
$env:TEMP = $Tmp
$env:TMP = $Tmp

$git = Get-Command git -ErrorAction SilentlyContinue
if ($git -and (Test-Path (Join-Path $Root ".git"))) {
    Write-Host "Mise à jour par Git..." -ForegroundColor Cyan
    $ErrorActionPreference = "Continue"  # git écrit sur la sortie d'erreur même quand tout va bien
    & git -C $Root fetch origin $Branch
    if ($LASTEXITCODE -ne 0) { throw "git fetch a échoué" }
    $present = @($Managed | Where-Object { & git -C $Root cat-file -e "origin/$($Branch):$_" 2>$null; $LASTEXITCODE -eq 0 })
    # checkout <ref> -- <chemins> : remplace ces fichiers, même modifiés localement.
    & git -C $Root checkout "origin/$Branch" -- @present
    if ($LASTEXITCODE -ne 0) { throw "git checkout a échoué" }
} else {
    Write-Host "Mise à jour par téléchargement (archive GitHub)..." -ForegroundColor Cyan
    $zip = Join-Path $Tmp "pipeline.zip"
    $url = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    } catch {
        throw "Téléchargement impossible ($url). Si le dépôt est privé, installe Git ou GitHub Desktop et clone le dépôt dans ce dossier."
    }
    $out = Join-Path $Tmp "pipeline_zip"
    if (Test-Path $out) { Remove-Item $out -Recurse -Force }
    Expand-Archive $zip -DestinationPath $out -Force
    $src = Get-ChildItem $out | Select-Object -First 1
    foreach ($p in $Managed) {
        $from = Join-Path $src.FullName $p
        if (-not (Test-Path $from)) { continue }
        $to = Join-Path $Root $p
        if ((Get-Item $from).PSIsContainer) {
            New-Item -ItemType Directory -Force $to | Out-Null
            Copy-Item (Join-Path $from "*") $to -Recurse -Force
        } else {
            Copy-Item $from $to -Force
        }
    }
    Remove-Item $out -Recurse -Force
    Remove-Item $zip -Force
}
# Anciens fichiers compilés : jamais réutilisés à tort.
Get-ChildItem (Join-Path $Root "texpipe") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
$ver = Get-Content (Join-Path $Root "texpipe\VERSION.txt") -ErrorAction SilentlyContinue | Select-Object -First 1
Write-Host "`nPipeline à jour : $ver" -ForegroundColor Green
