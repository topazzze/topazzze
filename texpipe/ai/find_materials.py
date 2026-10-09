"""
Recherche de matériaux photo CC0 (AmbientCG, Poly Haven) pour chaque zone.

Pour chaque zone de <nom>_zones.txt (sauf « emissif »), des matériaux
correspondant au type choisi (metal_sombre -> « black metal », « dark metal »,
« painted metal »...) sont cherchés sur AmbientCG et Poly Haven, téléchargés
en 1K dans une bibliothèque locale (réutilisée d'un asset à l'autre), puis
classés selon leur ressemblance avec la couleur de la zone.

Produit :
    <nom>_candidates.json        candidats par zone (chemins des textures)
    <nom>_materials_choice.txt   zone = matériau retenu (le 1er candidat) : À VALIDER

Les deux sources sont CC0 (usage commercial libre, sans attribution).

Usage (Python du pipeline, .venv) :
    python find_materials.py --mesh robot_uv.glb [--count 4]
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import zipfile

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from zones import read_zone_file, srgb_to_lab  # noqa: E402

USER_AGENT = "topazzze-texpipe/1.0 (pipeline de texturing ; contact : depot GitHub topazzze)"
ROOT = os.path.dirname(os.path.dirname(HERE))
LIBRARY = os.path.join(ROOT, "library")

# Recherche par type de matériau : requêtes AmbientCG, mots-clés Poly Haven
# (nom, étiquettes, catégories), et préférence de métal (1 = métal, 0 = non,
# None = indifférent, ex. métal peint).
SEARCH = {
    "metal_sombre": (["black metal", "dark metal", "painted metal", "metal"], ["metal", "black", "dark", "painted"], None),
    "metal_peint": (["painted metal", "metal paint"], ["painted", "metal", "paint"], None),
    "metal_nu": (["metal", "steel", "iron"], ["metal", "steel", "iron"], 1),
    "metal_brosse": (["brushed metal", "metal"], ["brushed", "metal"], 1),
    "chrome": (["chrome", "polished metal", "metal"], ["chrome", "polished", "metal"], 1),
    "or": (["gold"], ["gold"], 1),
    "cuivre": (["copper"], ["copper"], 1),
    "plastique": (["plastic"], ["plastic"], 0),
    "caoutchouc": (["rubber"], ["rubber"], 0),
    "pierre": (["rock", "stone"], ["rock", "stone"], 0),
    "beton": (["concrete"], ["concrete"], 0),
}

# Cartes reconnues par le suffixe du fichier (AmbientCG : Metal032_1K-JPG_Color.jpg ;
# Poly Haven, enregistré sous ph_<carte>.jpg : ph_diffuse.jpg, ph_nor_gl.jpg...).
MAP_KEYS = {
    "color": {"color", "diffuse", "diff", "basecolor", "albedo"},
    "normal": {"normalgl", "nor_gl"},
    "roughness": {"roughness", "rough"},
    "metalness": {"metalness", "metal"},
    "ao": {"ambientocclusion", "ao"},
    "height": {"displacement", "disp", "height"},
}


def log(msg):
    print(f"[find_materials] {msg}", flush=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="find_materials", description="Matériaux photo CC0 pour chaque zone.")
    p.add_argument("--mesh", required=True, help="Mesh de jeu (.glb) : sert à trouver le dossier et le nom")
    p.add_argument("--maps-dir", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--count", type=int, default=4, help="Candidats gardés par zone (défaut 4)")
    p.add_argument("--search", type=int, default=8, help="Candidats examinés par source et par zone (défaut 8)")
    p.add_argument("--query", action="append", default=[],
                   help="Recherche personnalisée pour une zone, ex. --query \"1=carbon fiber\" (répétable)")
    p.add_argument("--library", default=LIBRARY, help="Bibliothèque locale des matériaux téléchargés")
    p.add_argument("--sources", default="ambientcg,polyhaven")
    p.add_argument("--sheets", action="store_true", help="Assembler les rendus des candidats en planches légendées")
    p.add_argument("--final", action="store_true", help="Télécharger en haute résolution les matériaux retenus")
    p.add_argument("--resolution", default="2K", help="Résolution des matériaux retenus (1K, 2K, 4K ; défaut 2K)")
    a = p.parse_args(argv)
    a.mesh = os.path.abspath(a.mesh)
    a.maps_dir = os.path.abspath(a.maps_dir or os.path.dirname(a.mesh))
    if a.name is None:
        stem = os.path.splitext(os.path.basename(a.mesh))[0]
        a.name = stem[:-3] if stem.endswith("_uv") else stem
    a.sources = [s.strip() for s in a.sources.split(",") if s.strip()]
    a.library = os.path.abspath(a.library)
    return a


# -- réseau -------------------------------------------------------------------------------
def http(url, binary=False, tries=3):
    err = None
    for t in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            return data if binary else json.loads(data.decode("utf-8"))
        except Exception as e:  # réseau instable : nouvel essai
            err = e
            time.sleep(2 * (t + 1))
    raise RuntimeError(f"{url} : {err}")


# -- AmbientCG ----------------------------------------------------------------------------
def ambientcg_search(query, limit):
    url = "https://ambientcg.com/api/v2/full_json?" + urllib.parse.urlencode(
        dict(type="Material", q=query, sort="Popular", limit=limit, include="tagData,displayData"))
    data = http(url)
    out = []
    for asset in data.get("foundAssets", []):
        aid = asset.get("assetId")
        if aid:
            out.append(dict(source="ambientcg", id=aid, name=asset.get("displayName") or aid,
                            tags=[t.lower() for t in asset.get("tags", [])]))
    return out


def ambientcg_download(cand, folder, res="1K"):
    data = http(f"https://ambientcg.com/get?file={cand['id']}_{res}-JPG.zip", binary=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for member in z.namelist():
            if member.lower().endswith((".jpg", ".png")):
                with open(os.path.join(folder, os.path.basename(member)), "wb") as fh:
                    fh.write(z.read(member))


# -- Poly Haven ---------------------------------------------------------------------------
_PH_INDEX = {}


def polyhaven_search(words, limit):
    if not _PH_INDEX:
        _PH_INDEX.update(http("https://api.polyhaven.com/assets?type=textures"))
    scored = []
    for aid, info in _PH_INDEX.items():
        text = " ".join([aid, info.get("name", "")] + info.get("tags", []) + info.get("categories", [])).lower()
        hits = sum(1 for w in words if w in text)
        if hits:
            scored.append((hits, info.get("download_count", 0), aid, info))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [dict(source="polyhaven", id=aid, name=info.get("name", aid),
                 tags=[t.lower() for t in info.get("tags", [])], dimensions_mm=info.get("dimensions"))
            for _, _, aid, info in scored[:limit]]


def polyhaven_download(cand, folder, res="1k"):
    files = http(f"https://api.polyhaven.com/files/{cand['id']}")
    for key, by_res in files.items():
        low = key.lower()
        if low in ("blend", "gltf", "mtlx") or not isinstance(by_res, dict):
            continue
        entry = by_res.get(res) or by_res.get("2k") or next(iter(by_res.values()), None)
        if not isinstance(entry, dict):
            continue
        f = entry.get("jpg") or entry.get("png")
        if not f or "url" not in f:
            continue
        ext = os.path.splitext(f["url"])[1]
        with open(os.path.join(folder, f"ph_{low}{ext}"), "wb") as fh:
            fh.write(http(f["url"], binary=True))


# -- bibliothèque locale --------------------------------------------------------------------
def map_kind(filename):
    stem = os.path.splitext(filename)[0].lower()
    token = stem[3:] if stem.startswith("ph_") else stem.rsplit("_", 1)[-1]
    return next((key for key, names in MAP_KEYS.items() if token in names), None)


def find_maps(folder):
    maps = {}
    for f in sorted(os.listdir(folder)):
        if f.lower().endswith((".jpg", ".png")):
            kind = map_kind(f)
            if kind and kind not in maps:
                maps[kind] = os.path.join(folder, f)
    return maps


def fetch(cand, library):
    folder = os.path.join(library, f"{cand['source']}_{cand['id']}")
    if not os.path.isdir(folder) or not find_maps(folder).get("color"):
        os.makedirs(folder, exist_ok=True)
        (ambientcg_download if cand["source"] == "ambientcg" else polyhaven_download)(cand, folder)
    maps = find_maps(folder)
    if "color" not in maps:
        return None
    rgb = np.asarray(Image.open(maps["color"]).convert("RGB").resize((64, 64)), np.float32) / 255
    metal = None
    if "metalness" in maps:
        metal = float(np.asarray(Image.open(maps["metalness"]).convert("L").resize((64, 64)), np.float32).mean() / 255)
    return dict(cand, folder=folder, maps=maps, mean_color="#%02x%02x%02x" % tuple(int(c * 255) for c in rgb.mean((0, 1))),
                lab=srgb_to_lab(rgb.reshape(-1, 3)).mean(0).tolist(), metal=metal)


def rank(cands, target_rgb, want_metal):
    """Plus petit = meilleur : écart de couleur (Lab) + respect du métal."""
    tl = srgb_to_lab(np.array(target_rgb, np.float32)[None])[0] if target_rgb is not None else None
    scored = []
    for c in cands:
        s = 0.0
        if tl is not None:
            s += float(np.linalg.norm(np.array(c["lab"]) - tl))
        if want_metal is not None:
            m = c["metal"] if c["metal"] is not None else 0.0
            s += 40.0 * abs(m - want_metal)
        scored.append((s, c))
    scored.sort(key=lambda t: t[0])
    return [dict(c, score=round(s, 1)) for s, c in scored]


def run(a):
    base = os.path.join(a.maps_dir, a.name)
    zones = read_zone_file(base + "_zones.txt")
    custom = {}
    for q in a.query:
        zid, _, text = q.partition("=")
        custom[int(zid)] = text.strip()
    os.makedirs(a.library, exist_ok=True)
    result, choice = {}, []
    for zid, (mat, col) in sorted(zones.items()):
        if mat == "emissif":
            choice.append(f"{zid} = emissif")
            continue
        queries, words, want_metal = SEARCH[mat]
        if zid in custom:
            queries, words = [custom[zid]], custom[zid].lower().split()
        found, seen = [], set()
        if "ambientcg" in a.sources:
            for q in queries:
                try:
                    for c in ambientcg_search(q, a.search):
                        if c["id"] not in seen and len([f for f in found if f["source"] == "ambientcg"]) < a.search:
                            seen.add(c["id"])
                            found.append(c)
                except RuntimeError as e:
                    log(f"AmbientCG indisponible ({e})")
                    break
        if "polyhaven" in a.sources:
            try:
                for c in polyhaven_search(words, a.search):
                    if c["id"] not in seen:
                        seen.add(c["id"])
                        found.append(c)
            except RuntimeError as e:
                log(f"Poly Haven indisponible ({e})")
        log(f"Zone {zid} ({mat}) : {len(found)} matériaux trouvés, téléchargement 1K...")
        ready = []
        for c in found:
            try:
                r = fetch(c, a.library)
                if r is not None:
                    ready.append(r)
            except Exception as e:
                log(f"  {c['source']}:{c['id']} ignoré ({e})")
        ranked = rank(ready, col, want_metal)[: a.count]
        area = next((float(l.split("#")[-1].split("%")[0]) for l in open(base + "_zones.txt", encoding="utf-8")
                     if l.split("=")[0].strip() == str(zid) and "%" in l), 100.0)
        result[str(zid)] = dict(material=mat, area_percent=area, color=("#%02x%02x%02x" % tuple(int(c * 255) for c in col)) if col else None,
                                candidates=ranked)
        for i, c in enumerate(ranked):
            log(f"  {i + 1}. {c['source']}:{c['id']:<28} {c['mean_color']}  (écart {c['score']})")
        choice.append(f"{zid} = {ranked[0]['source']}:{ranked[0]['id']}" if ranked else f"{zid} = procedural")
    with open(base + "_candidates.json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    lines = ["# Matériau retenu pour chaque zone (modifiable) :",
             "#   source:identifiant   (voir les images <nom>_zoneN_candidats.png)",
             "#   procedural           (matériau calculé de l'étape 6)",
             "#   emissif              (lumière, couleur de la zone)",
             "# Option après le nom : taille=0.5  (taille d'un motif en mètres, défaut 0.5)", ""] + choice
    with open(base + "_materials_choice.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"Choix proposés -> {base}_materials_choice.txt")
    return result


def read_choice(path):
    out = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                zid, rest = line.split("=", 1)
                out[int(zid)] = rest.split()[0] if rest.split() else "procedural"
    return out


def make_sheets(a):
    """Planches légendées des rendus de chaque zone (un rendu par candidat)."""
    from zones import get_font

    base = os.path.join(a.maps_dir, a.name)
    cands = json.load(open(base + "_candidates.json", encoding="utf-8"))
    choice = read_choice(base + "_materials_choice.txt")
    rows = []
    for zs, info in sorted(cands.items(), key=lambda t: int(t[0])):
        z = int(zs)
        tiles = []
        for k, c in enumerate(info["candidates"]):
            path = os.path.join(base + "_renders", f"zone{z}_{k}.png")
            if not os.path.exists(path):
                continue
            im = Image.open(path).convert("RGB")
            w, h = im.size
            tile = Image.new("RGB", (w, h + 70), (28, 28, 30))
            tile.paste(im, (0, 0))
            sw = Image.open(c["maps"]["color"]).convert("RGB").resize((96, 96))
            tile.paste(sw, (w - 104, 8))
            d = ImageDraw.Draw(tile)
            key = f"{c['source']}:{c['id']}"
            if choice.get(z) == key:
                d.rectangle([2, 2, w - 3, h + 67], outline=(80, 220, 120), width=5)
            d.text((10, h + 8), f"{k + 1}. {key}", fill=(240, 240, 240), font=get_font(20))
            d.text((10, h + 38), f"{c['name']}  -  {c['mean_color']}", fill=(170, 170, 170), font=get_font(16))
            tiles.append(tile)
        if not tiles:
            continue
        row = Image.new("RGB", (sum(t.width for t in tiles), tiles[0].height + 44), (20, 20, 22))
        ImageDraw.Draw(row).text((10, 10), f"Zone {z} - {info['material']}  (cadre vert = choix actuel)",
                                 fill=(255, 255, 255), font=get_font(22))
        x = 0
        for t in tiles:
            row.paste(t, (x, 44))
            x += t.width
        row.save(f"{base}_zone{z}_candidats.png")
        rows.append(row)
        log(f"Planche : {base}_zone{z}_candidats.png")
    if rows:
        W = max(r.width for r in rows)
        sheet = Image.new("RGB", (W, sum(r.height for r in rows)), (20, 20, 22))
        y = 0
        for r in rows:
            sheet.paste(r, (0, y))
            y += r.height
        sheet.save(f"{base}_materiaux_candidats.png")
        log(f"Planche générale : {base}_materiaux_candidats.png")


def finalize(a):
    """Télécharge en 2K les matériaux retenus dans <nom>_materials_choice.txt."""
    base = os.path.join(a.maps_dir, a.name)
    cands = json.load(open(base + "_candidates.json", encoding="utf-8")) if os.path.exists(base + "_candidates.json") else {}
    final = {}
    for z, pick in read_choice(base + "_materials_choice.txt").items():
        if ":" not in pick:
            continue
        source, aid = pick.split(":", 1)
        known = next((c for c in cands.get(str(z), {}).get("candidates", []) if c["source"] == source and c["id"] == aid),
                     dict(source=source, id=aid, name=aid))
        folder = os.path.join(a.library, f"{source}_{aid}_{a.resolution}")
        if not os.path.isdir(folder) or "color" not in find_maps(folder):
            os.makedirs(folder, exist_ok=True)
            log(f"Zone {z} : téléchargement {pick} en {a.resolution}...")
            try:
                if source == "ambientcg":
                    ambientcg_download(known, folder, a.resolution.upper())
                else:
                    polyhaven_download(known, folder, a.resolution.lower())
            except Exception as e:
                log(f"  échec ({e}) : version 1K conservée")
                folder = known.get("folder", folder)
        maps = find_maps(folder)
        if "color" in maps:
            final[str(z)] = dict(known, folder=folder, maps=maps)
    with open(base + "_materials_final.json", "w", encoding="utf-8") as fh:
        json.dump(final, fh, indent=2, ensure_ascii=False)
    log(f"Matériaux retenus -> {base}_materials_final.json")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    a = parse_args(argv)
    if a.sheets:
        return make_sheets(a)
    if a.final:
        return finalize(a)
    return run(a)


if __name__ == "__main__":
    main()
