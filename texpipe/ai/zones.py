"""
Étape 5 du pipeline : découpage en zones de matériau.

Les pixels de la texture sont regroupés selon leur couleur (guide IA de
l'étape 4, couleur Tripo) et leurs propriétés Tripo (métal, rugosité). Chaque
zone reçoit une proposition de matériau, à valider ou corriger dans un petit
fichier texte (1 à 2 minutes par asset).

Produit :
    <nom>_zones.txt          zone -> matériau (+ couleur) : À VÉRIFIER / MODIFIER
    <nom>_zones_preview.png  l'objet vu de 4 côtés, zones numérotées
    <nom>_zones.png          les zones sur la texture (couleurs)
    <nom>_zones_id.png       les zones sur la texture (numéros, pour l'étape 6)

Usage (Python du pipeline, .venv) :
    python zones.py --mesh robot_uv.glb [--zones 6]
"""

import argparse
import json
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import color_guide as cg  # noqa: E402
from imgops import box_blur, fill_invalid, save_png  # noqa: E402

# Matériaux connus de l'étape 6 (texpipe/ai/materials.py).
MATERIALS = ["metal_peint", "metal_nu", "metal_sombre", "metal_brosse", "chrome", "or", "cuivre",
             "plastique", "caoutchouc", "pierre", "beton", "emissif"]
PALETTE = np.array([(230, 25, 75), (60, 180, 75), (255, 225, 25), (0, 130, 200), (245, 130, 48),
                    (145, 30, 180), (70, 240, 240), (240, 50, 230), (210, 245, 60), (250, 190, 212),
                    (0, 128, 128), (170, 110, 40)], dtype=np.float32) / 255


def log(msg):
    print(f"[zones] {msg}", flush=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="zones", description="Découpage en zones de matériau.")
    p.add_argument("--mesh", required=True, help="Mesh de jeu avec ses UV (.glb, étape 2)")
    p.add_argument("--maps-dir", default=None, help="Dossier des cartes des étapes 3-4 (défaut : celui du mesh)")
    p.add_argument("--name", default=None, help="Préfixe des cartes (défaut : nom du mesh sans « _uv »)")
    p.add_argument("--zones", type=int, default=0, help="Nombre de zones (défaut : choisi automatiquement, 3 à 8)")
    p.add_argument("--size", type=int, default=2048, help="Résolution de la carte des zones (défaut 2048)")
    p.add_argument("--smooth", type=int, default=3, help="Lissage des zones en pixels (défaut 3)")
    p.add_argument("--merge", type=float, default=12, help="Zones fusionnées si leurs couleurs diffèrent de moins que ça (Delta E, défaut 12)")
    p.add_argument("--glow-chroma", type=float, default=30, help="Parties lumineuses : vivacité minimale (Lab, défaut 30)")
    p.add_argument("--glow-light", type=float, default=30, help="Parties lumineuses : clarté minimale (Lab, défaut 30)")
    p.add_argument("--unlit", nargs="*", default=None,
                   help="Images sans éclairage (aplats de couleur). Défaut : trouvées dans le dossier "
                        "(nom contenant « unlit », ex. Archange_Front_Unlit.png)")
    p.add_argument("--bg-tolerance", type=int, default=12, help="Détourage du fond des images unlit")
    p.add_argument("--hidden-fill", default="main", choices=["main", "neighbors"],
                   help="Parties vues par aucune image unlit : main (défaut) = matériau principal ; "
                        "neighbors = prolongement des zones voisines")
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)
    a.mesh = os.path.abspath(a.mesh)
    a.maps_dir = os.path.abspath(a.maps_dir or os.path.dirname(a.mesh))
    if a.name is None:
        stem = os.path.splitext(os.path.basename(a.mesh))[0]
        a.name = stem[:-3] if stem.endswith("_uv") else stem
    return a


# -- outils ---------------------------------------------------------------------------
def load_map(path, size, gray=False):
    if not os.path.exists(path):
        return None
    im = Image.open(path)
    if im.mode.startswith("I"):  # 16 bits
        arr = np.asarray(im, dtype=np.float32) / 65535
        im = Image.fromarray((arr * 255).astype(np.uint8))
    im = im.convert("L" if gray else "RGB").resize((size, size), Image.BILINEAR)
    return np.asarray(im, dtype=np.float32) / 255


def srgb_to_lab(rgb):
    c = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.9505, 1.0, 1.089])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def kmeans(x, k, rng, iters=25):
    """k-moyennes (initialisation k-means++)."""
    cent = [x[rng.integers(len(x))]]
    for _ in range(1, k):
        d = np.min(((x[:, None] - np.array(cent)[None]) ** 2).sum(-1), 1)
        if d.sum() <= 1e-9:  # moins de couleurs distinctes que de zones demandées
            break
        cent.append(x[rng.choice(len(x), p=d / d.sum())])
    cent = np.array(cent)
    k = len(cent)
    for _ in range(iters):
        lab = np.argmin(((x[:, None] - cent[None]) ** 2).sum(-1), 1)
        new = np.array([x[lab == j].mean(0) if (lab == j).any() else cent[j] for j in range(k)])
        if np.allclose(new, cent):
            break
        cent = new
    inertia = float(((x - cent[lab]) ** 2).sum())
    return cent, inertia


def assign(x, cent, chunk=1_000_000):
    out = np.empty(len(x), dtype=np.int64)
    for s in range(0, len(x), chunk):
        out[s : s + chunk] = np.argmin(((x[s : s + chunk, None] - cent[None]) ** 2).sum(-1), 1)
    return out


def majority(labels, valid, k, r):
    """Chaque pixel prend la zone la plus présente autour de lui."""
    votes = np.stack([box_blur(((labels == j) & valid).astype(np.float32), r) for j in range(k)], -1)
    return np.argmax(votes, -1)


# -- découpage --------------------------------------------------------------------------
def guess_material(lab, metal, rough):
    """Proposition de matériau d'après la couleur moyenne (Lab) et les
    valeurs Tripo de métal / rugosité (None si absentes)."""
    L, chroma = lab[0], float(np.hypot(lab[1], lab[2]))
    if chroma > 35 and L > 30:
        return "emissif"
    if metal is not None and metal > 0.5:
        if chroma > 15:
            return "metal_peint"
        return "metal_sombre" if L < 35 else ("chrome" if rough is not None and rough < 0.15 else "metal_nu")
    if L < 18 and chroma < 8:
        return "caoutchouc"
    return "metal_peint"


def run(a):
    t0 = time.time()
    base = os.path.join(a.maps_dir, a.name)
    size = a.size
    guide = load_map(f"{base}_color_guide.png", size)
    tripo = load_map(f"{base}_basecolor_high.png", size)
    metal = load_map(f"{base}_metallic_high.png", size, gray=True)
    rough = load_map(f"{base}_roughness_high.png", size, gray=True)
    if guide is None and tripo is None:
        raise SystemExit("Ni couleur guide (étape 4) ni couleur Tripo (étape 3) trouvée pour " + base)

    tri_p, tri_n, tri_uv = cg.load_glb(a.mesh)
    _, _, covered = cg.texel_maps(tri_p, tri_n, tri_uv, size)

    feats = []
    for img in (guide, tripo):
        if img is not None:
            # La luminosité dépend beaucoup de l'éclairage des images (reflets) :
            # la teinte compte plus.
            feats.append(srgb_to_lab(img) * np.array([0.5, 1.0, 1.0]))
    for img in (metal, rough):
        # Valeurs Tripo utilisées seulement si elles varient sur l'objet.
        if img is not None and img[covered].std() > 0.08:
            feats.append(img[..., None] * 60.0)  # même ordre de grandeur que L* (0-100)
    feat = np.concatenate(feats, -1)
    # Parties lumineuses (lignes néon, voyants) : couleur vive ET claire, sur
    # quelques % de la surface ; détectées à part, sinon noyées dans les autres.
    emis = np.zeros_like(covered)
    for img in (guide, tripo):
        if img is not None:
            lab_i = srgb_to_lab(img)
            emis |= (np.hypot(lab_i[..., 1], lab_i[..., 2]) > a.glow_chroma) & (lab_i[..., 0] > a.glow_light)
    emis &= covered
    emis = (box_blur(emis.astype(np.float32), 1) > 0.5) & covered  # sans les pixels isolés
    if emis.mean() < 0.002 * covered.mean():
        emis[:] = False
    unlit = find_unlit(a)
    if unlit:
        ulab, useen, ucolors = project_unlit(unlit, tri_p, tri_n, tri_uv, size, a)
        if ulab is not None:
            lab, k, mats = zones_from_unlit(a, ulab, useen, covered, emis if emis.any() else None, ucolors)
            onehot = np.stack([(lab == j) & covered for j in range(k)], -1).astype(np.float32)
            lab = np.argmax(fill_invalid(onehot, covered), -1)
            area = np.array([(lab[covered] == j).mean() for j in range(k)])
            order = np.argsort(-area)
            remap = np.empty(k, dtype=np.int64)
            remap[order] = np.arange(k)
            lab = remap[lab]
            zones = []
            for z, j in enumerate(order):
                col = ucolors[j]
                zones.append(dict(id=z + 1, material=mats[j], color="#%02x%02x%02x" % tuple(int(c * 255) for c in col),
                                  area_percent=round(100 * float(area[j]), 1)))
            return write_outputs(a, base, t0, lab, covered, zones, tri_p, tri_n, tri_uv)
    rest = covered & ~emis
    sel = np.nonzero(rest)
    x = feat[sel]
    rng = np.random.default_rng(a.seed)
    sample = x[rng.choice(len(x), size=min(len(x), 40000), replace=False)]

    if a.zones > 0:
        k = a.zones
        cent, _ = kmeans(sample, k, rng)
    else:
        # Nombre de zones : on s'arrête quand une zone de plus n'améliore
        # presque plus le regroupement.
        prev, results = None, {}
        for k in range(2, 9):
            results[k] = kmeans(sample, k, np.random.default_rng(a.seed + k))
            if prev is not None and results[k][1] > 0.82 * prev and k > 3:
                k -= 1
                break
            prev = results[k][1]
        cent = results[k][0]
    labels = np.full((size, size), -1, dtype=np.int64)
    labels[sel] = assign(x, cent)

    # Lissage (taches isolées), puis zone lumineuse, puis extension au débord.
    lab = np.maximum(labels, 0)
    for _ in range(2):
        lab = np.where(rest, majority(lab, rest, k, a.smooth), lab)
    # Zones de couleurs presque identiques (même matière, éclairage différent
    # dans les images) : fusionnées.
    ref_img = guide if guide is not None else tripo
    full_lab = srgb_to_lab(ref_img)
    med = np.array([np.median(full_lab[rest & (lab == j)], 0) if (rest & (lab == j)).any() else np.full(3, 1e6)
                    for j in range(k)])
    parent = list(range(k))
    for i in range(k):
        for j in range(i + 1, k):
            if np.linalg.norm(med[i] - med[j]) < a.merge:
                ri, rj = parent[i], parent[j]
                parent = [ri if p_ == rj else p_ for p_ in parent]
    roots = sorted(set(parent))
    lab = np.array([roots.index(parent[j]) for j in range(k)])[lab]
    k = len(roots)
    glow_zone = None
    if emis.any():
        glow_zone = k
        lab[emis] = k
        k += 1
    onehot = np.stack([(lab == j) & covered for j in range(k)], -1).astype(np.float32)
    lab = np.argmax(fill_invalid(onehot, covered), -1)

    # Zones triées par surface (1 = la plus grande).
    area = np.array([(lab[covered] == j).mean() for j in range(k)])
    order = np.argsort(-area)
    remap = np.empty(k, dtype=np.int64)
    remap[order] = np.arange(k)
    lab = remap[lab]
    area = area[order]

    zones = []
    for z in range(k):
        m = covered & (lab == z)
        if not m.any():
            continue
        src = tripo if tripo is not None else guide
        col = np.median(src[m], 0)
        glab = srgb_to_lab((guide if guide is not None else tripo)[m])
        # Pixels les plus vifs de la zone (le cœur des lignes lumineuses).
        chroma = np.hypot(glab[:, 1], glab[:, 2])
        top = chroma >= np.percentile(chroma, 75)
        glab = np.array([np.median(glab[top, 0]), np.median(glab[top, 1]), np.median(glab[top, 2])])
        mat = guess_material(glab, float(np.median(metal[m])) if metal is not None else None,
                             float(np.median(rough[m])) if rough is not None else None)
        if glow_zone is not None and remap[glow_zone] == z:
            mat = "emissif"
        elif mat == "emissif":
            mat = "metal_peint"  # couleur vive mais pas lumineuse
        if mat == "emissif":
            col = np.percentile((guide if guide is not None else tripo)[m], 90, axis=0)
        zones.append(dict(id=z + 1, material=mat, color="#%02x%02x%02x" % tuple(int(c * 255) for c in col),
                          area_percent=round(100 * float(area[z]), 1)))

    return write_outputs(a, base, t0, lab, covered, zones, tri_p, tri_n, tri_uv)


def write_outputs(a, base, t0, lab, covered, zones, tri_p, tri_n, tri_uv):
    save_png(f"{base}_zones.png", np.where(covered[..., None], PALETTE[lab % len(PALETTE)], 0.1)[::-1])
    save_png(f"{base}_zones_id.png", ((lab + 1) * covered / 255.0)[::-1])
    write_zone_file(f"{base}_zones.txt", a.name, zones)
    render_preview(f"{base}_zones_preview.png", tri_p, tri_n, tri_uv, lab, covered, zones)
    with open(f"{base}_zones_report.json", "w", encoding="utf-8") as fh:
        json.dump(dict(zones=zones, seconds=round(time.time() - t0, 1)), fh, indent=2, ensure_ascii=False)
    for zd in zones:
        log(f"  zone {zd['id']} : {zd['material']:<12} {zd['color']}  ({zd['area_percent']} %)")
    log(f"{len(zones)} zones -> {base}_zones.txt (à vérifier), aperçu {base}_zones_preview.png "
        f"({time.time() - t0:.0f} s)")
    return zones


# -- images « unlit » (aplats de couleur, sans éclairage) ------------------------------------
def find_unlit(a):
    """Images sans éclairage de l'asset : nom contenant « unlit » et
    commençant comme le mesh (Archange_Front_Unlit.png pour Archange_LowPoly).
    Le côté (front, back, left, right) est lu dans le nom, face par défaut."""
    import re

    paths = list(a.unlit or [])
    if not paths:
        prefix = re.split(r"[_\- ]", a.name)[0].lower()
        for f in sorted(os.listdir(a.maps_dir)):
            low = f.lower()
            if "unlit" in low and low.startswith(prefix) and low.rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "webp"):
                paths.append(os.path.join(a.maps_dir, f))
    out = []
    for pth in paths:
        low = os.path.basename(pth).lower()
        side = next((sd for sd in ("back", "left", "right", "front") if sd in low), "front")
        out.append((side, pth))
    # Ordre de priorité : la face (référence principale), puis le dos, puis les côtés.
    rank = {"front": 0, "back": 1, "left": 2, "right": 3}
    return sorted(out, key=lambda t: rank[t[0]])


def project_unlit(unlit, tri_p, tri_n, tri_uv, size, a, view_size=1024):
    """Recale chaque image unlit sur la silhouette du mesh (vue du même côté),
    classe ses couleurs en zones sur l'image même, puis reprojette les zones
    (et non les couleurs : aucun mélange aux bords) sur les UV.
    Renvoie (zones par pixel de texture, pixels vus, couleurs des zones RGB)."""
    flat = tri_p.reshape(-1, 3)
    c = (flat.min(0) + flat.max(0)) / 2
    tp = (tri_p - c) * (0.5 / np.abs(flat - c).max())
    views = cg.render_views(tp, tri_n, view_size)
    imgs, vws, masks = [], [], []
    for side, path in unlit:
        img = Image.open(path)
        img = img if img.mode == "RGBA" else cg.remove_background(img, a.bg_tolerance)
        v = cg.SIDE_VIEWS[side]
        cands = [v] + ([4 - v] if side in ("left", "right") else [])
        tries = [(cg.align_reference(img, views[cv]["mask"], view_size), cv) for cv in cands]
        (rgb, mask, iou), cv = max(tries, key=lambda t: t[0][2] + (0.05 if t[1] == v else 0))
        log(f"Image unlit {os.path.basename(path)} -> vue {cg.VIEW_NAMES[cv]} (superposition {100 * iou:.0f} %)")
        if iou < 0.7:
            log("  superposition trop faible : image ignorée (cadrage ou pose différents du mesh)")
            continue
        imgs.append(rgb)
        vws.append(views[cv])
        masks.append(cg.erode(mask, 1))
    if not imgs:
        return None, None, None
    labels, colors = classify_flat_colors([im[m] for im, m in zip(imgs, masks)], a)
    k = len(colors)
    onehots = []
    for im, m, lab_px in zip(imgs, masks, labels):
        oh = np.zeros(im.shape[:2] + (k,), dtype=np.float32)
        ys, xs = np.nonzero(m)
        oh[ys, xs, lab_px] = 1.0
        onehots.append(oh)
    pos, nrm, covered = cg.texel_maps(tp, tri_n, tri_uv, size)
    votes, seen = cg.back_project(onehots, vws, pos, nrm, covered, masks, priority=True)
    return np.argmax(votes, -1), seen & covered, colors


def classify_flat_colors(pixel_sets, a):
    """Couleurs d'aplats -> zones. Les teintes intermédiaires (bords
    anticrénelés entre deux aplats) sont rattachées à l'aplat le plus proche."""
    allpx = np.concatenate(pixel_sets)
    lab_all = srgb_to_lab(allpx)
    rng = np.random.default_rng(a.seed)
    sample = lab_all[rng.choice(len(lab_all), size=min(len(lab_all), 40000), replace=False)]
    cent, _ = kmeans(sample, a.zones or 8, rng)
    asg = assign(lab_all, cent)
    k = len(cent)
    size = np.bincount(asg, minlength=k).astype(float)
    cl = np.array([np.median(lab_all[asg == j], 0) if size[j] else cent[j] for j in range(k)])
    alive = [j for j in range(k) if size[j] > 0]
    target = {j: j for j in range(k)}
    changed = True
    while changed:
        changed = False
        alive.sort(key=lambda j: size[j])
        for m in alive:
            others = [j for j in alive if j != m and size[j] > size[m]]
            best = None
            for ii, i in enumerate(others):
                if np.linalg.norm(cl[i] - cl[m]) < a.merge:  # presque la même couleur
                    best = (0.0, i, i, 0.0)
                    break
                for j in others[ii + 1 :]:
                    seg = cl[j] - cl[i]
                    t = float(np.dot(cl[m] - cl[i], seg) / max(np.dot(seg, seg), 1e-9))
                    d = float(np.linalg.norm(cl[i] + np.clip(t, 0, 1) * seg - cl[m]))
                    # t < 0 : dépassement au-delà d'un aplat (rebond de redimensionnement).
                    if -0.6 < t < 0.9 and d < 10 and (best is None or d < best[0]):
                        best = (d, i, j, t)
            if best is not None:
                _, i, j, t = best
                dest = i if t < 0.5 else j
                for key, val in target.items():
                    if val == m:
                        target[key] = dest
                size[dest] += size[m]
                size[m] = 0
                alive.remove(m)
                changed = True
                break
    # Gris sans teinte : contours, ombrages et reflets du dessin. Chacun est
    # rattaché soit à la couleur sombre principale (le corps), soit au gris
    # principal plus clair (rotules...), selon sa clarté.
    neutral = [j for j in alive if np.hypot(cl[j][1], cl[j][2]) < 10]
    if len(neutral) > 1:
        body = max(neutral, key=lambda j: size[j])
        lighter = [j for j in neutral if cl[j][0] > cl[body][0] + 20]
        gray = max(lighter, key=lambda j: size[j]) if lighter else None
        for m in [j for j in neutral if j not in (body, gray)]:
            if gray is None:
                dest = body if abs(cl[m][0] - cl[body][0]) < 20 else None
            else:
                cut = cl[body][0] + 0.65 * (cl[gray][0] - cl[body][0])
                dest = body if cl[m][0] < cut else gray
            if dest is None:
                continue
            for key, val in target.items():
                if val == m:
                    target[key] = dest
            size[dest] += size[m]
            size[m] = 0
            alive.remove(m)
    # Couleurs presque absentes (pixels de bord mêlés au fond) : rattachées à
    # la couleur restante la plus proche.
    tiny = [j for j in alive if size[j] < 0.003 * size.sum()]
    big = [j for j in alive if j not in tiny]
    for m in tiny if big else []:
        dest = min(big, key=lambda j: np.linalg.norm(cl[j] - cl[m]))
        for key, val in target.items():
            if val == m:
                target[key] = dest
        alive.remove(m)
    keep = sorted(alive)
    final = np.array([keep.index(target[j]) for j in range(k)])
    asg = final[asg]
    colors = np.array([np.median(allpx[asg == j], 0) for j in range(len(keep))])
    out, s0 = [], 0
    for ps in pixel_sets:
        out.append(asg[s0 : s0 + len(ps)])
        s0 += len(ps)
    return out, colors


def guess_unlit(lab):
    """Matériau d'après une couleur sans éclairage (Lab)."""
    L, chroma = lab[0], float(np.hypot(lab[1], lab[2]))
    if chroma > 35 and L > 25:
        return "emissif"
    if chroma < 12:
        return "metal_sombre" if L < 25 else "metal_nu"
    return "metal_peint"


def zones_from_unlit(a, lab, seen, covered, emis, colors):
    """Complète les zones des images unlit sur les parties qu'elles ne voient
    pas : lignes lumineuses reprises des images éclairées (étape 4), le reste
    étendu depuis les zones voisines."""
    k = len(colors)
    mats = [guess_unlit(srgb_to_lab(c)) for c in colors]
    for r in (1, 2):  # taches isolées retirées ; les LED (plusieurs pixels de large) restent
        lab = np.where(seen, majority(lab, seen, k, r), lab)
    hidden = covered & ~seen
    glow = [j for j in range(k) if mats[j] == "emissif"]
    if a.hidden_fill == "main":
        # Parties invisibles : matériau principal (la plus grande zone).
        main = int(np.argmax([(seen & (lab == j)).sum() for j in range(k)]))
        lab = np.where(seen, lab, main)
    else:
        fill_src = seen & ~np.isin(lab, glow) if glow else seen
        onehot = np.stack([(lab == j) & fill_src for j in range(k)], -1).astype(np.float32)
        filled = np.argmax(fill_invalid(onehot, fill_src), -1)
        lab = np.where(seen, lab, filled)
    # Secours par les images éclairées seulement si les images unlit laissent
    # beaucoup de surface invisible (sinon, les reflets y passent pour des LED).
    if glow and emis is not None and hidden.sum() > 0.2 * covered.sum():
        lab[hidden & emis] = max(glow, key=lambda j: (seen & (lab == j)).sum())
    if hidden.any():
        log(f"{100 * hidden.sum() / covered.sum():.0f} % de la surface n'est vue par aucune image unlit "
            "(creux, dessus, dessous...) : " + ("matériau principal" if a.hidden_fill == "main" else "zones voisines"))
    return lab, k, mats




def write_zone_file(path, name, zones):
    lines = [
        f"# Zones de matériau de {name}",
        "# Une ligne par zone :  numéro = matériau  #couleur",
        "# Modifier le matériau et/ou la couleur si besoin, puis lancer l'étape 6.",
        "# Matériaux : " + ", ".join(MATERIALS),
        "# Plusieurs zones peuvent avoir le même matériau. La couleur (#RRGGBB) est",
        "# facultative : sans elle, la couleur du matériau par défaut est utilisée.",
        "",
    ]
    for z in zones:
        lines.append(f"{z['id']} = {z['material']:<12} {z['color']}    # {z['area_percent']} % de la surface")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def read_zone_file(path):
    """{numéro: (matériau, couleur RGB 0-1 ou None)}. Lignes « 3 = metal_nu
    #8a8a8a  # commentaire » ; les lignes commençant par # sont ignorées."""
    import re

    out = {}
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                raise SystemExit(f"{path}, ligne {n} : « numéro = matériau » attendu")
            zid, rest = line.split("=", 1)
            words = rest.split()
            mat = words[0] if words else ""
            if mat not in MATERIALS:
                raise SystemExit(f"{path}, ligne {n} : matériau inconnu « {mat} ». Possibles : {', '.join(MATERIALS)}")
            col = None
            if len(words) > 1 and re.fullmatch(r"#[0-9a-fA-F]{6}", words[1]):
                col = tuple(int(words[1][i : i + 2], 16) / 255 for i in (1, 3, 5))
            out[int(zid)] = (mat, col)
    return out


def get_font(size):
    """Police avec accents (Arial sous Windows, DejaVu sous Linux)."""
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_preview(path, tri_p, tri_n, tri_uv, lab, covered, zones, size=640):
    """L'objet vu de face, gauche, dos et droite, zones en couleur et
    numérotées, avec la légende."""
    flat = tri_p.reshape(-1, 3)
    c = (flat.min(0) + flat.max(0)) / 2
    tp = (tri_p - c) * (0.5 / np.abs(flat - c).max())
    S = lab.shape[0]
    panels = []
    font = get_font(22)
    for v, cam in enumerate(cg.cameras()[:4]):
        xy, depth = cg.project(tp, cam, size)
        tri_id, bary, _ = cg.rasterize(xy, depth, size, size)
        uv = cg.interpolate(tri_uv, tri_id, bary)
        nrm = cg.interpolate(tri_n, tri_id, bary)
        nrm /= np.maximum(np.linalg.norm(nrm, axis=-1, keepdims=True), 1e-9)
        px = np.clip((uv[..., 0] * S).astype(int), 0, S - 1)
        py = np.clip((uv[..., 1] * S).astype(int), 0, S - 1)
        z = lab[py, px]
        shade = 0.45 + 0.55 * np.clip(np.abs(nrm @ cam[3]), 0, 1)
        img = np.where((tri_id >= 0)[..., None], PALETTE[z % len(PALETTE)] * shade[..., None], 0.16)
        im = Image.fromarray((img * 255).astype(np.uint8))
        d = ImageDraw.Draw(im)
        d.text((8, 6), cg.VIEW_NAMES[v], fill=(220, 220, 220), font=font)
        for zd in zones:
            m = (tri_id >= 0) & (z == zd["id"] - 1)
            if m.sum() < 0.002 * size * size:
                continue
            core = m
            while True:  # point le plus loin du bord de la zone
                nxt = cg.erode(core, 2)
                if not nxt.any():
                    break
                core = nxt
            ys, xs = np.nonzero(core)
            j = len(ys) // 2
            d.text((xs[j], ys[j]), str(zd["id"]), fill=(255, 255, 255), font=font, anchor="mm",
                   stroke_width=3, stroke_fill=(0, 0, 0))
        panels.append(np.asarray(im))
    row = np.concatenate(panels, 1)
    legend = Image.new("RGB", (row.shape[1], 40), (30, 30, 30))
    d = ImageDraw.Draw(legend)
    x = 10
    for zd in zones:
        col = tuple(int(c * 255) for c in PALETTE[(zd["id"] - 1) % len(PALETTE)])
        d.rectangle([x, 10, x + 20, 30], fill=col)
        label = f"{zd['id']} {zd['material']} ({zd['area_percent']} %)"
        d.text((x + 26, 20), label, fill=(230, 230, 230), font=get_font(16), anchor="lm")
        x += 40 + int(d.textlength(label, font=get_font(16)))
    out = np.concatenate([row, np.asarray(legend)], 0)
    Image.fromarray(out).save(path)


def main(argv=None):
    return run(parse_args(argv))


if __name__ == "__main__":
    main()
