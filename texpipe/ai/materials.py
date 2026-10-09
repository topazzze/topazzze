"""
Étape 6 du pipeline : composition des matériaux.

Chaque zone (étape 5, fichier <nom>_zones.txt) reçoit un matériau PBR
procédural (métal, peinture, pierre...), puis des couches d'usure guidées par
les cartes de l'étape 3 :
    - arêtes usées (courbure convexe) : la peinture laisse voir le métal ;
    - saleté dans les creux (occlusion, courbure concave, bas de l'objet) ;
    - poussière sur les surfaces tournées vers le haut.
Les variations viennent d'un bruit 3D évalué sur la surface : aucune couture
aux bords des îlots UV, et la symétrie du mesh est respectée.

Produit (textures pour Unreal, sur les UV du mesh de jeu) :
    T_<nom>_BC.png    couleur de base (sRGB)
    T_<nom>_N.png     normal map (DirectX), relief du HighPoly + micro-relief
    T_<nom>_ORM.png   R = occlusion, G = rugosité, B = métal (linéaire)
    T_<nom>_E.png     émission (si une zone est « emissif »)

Usage (Python du pipeline, .venv) :
    python materials.py --mesh robot_uv.glb [--wear 0.5 --dirt 0.5 --dust 0.2]
"""

import argparse
import json
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
import color_guide as cg  # noqa: E402
from imgops import pipeline_version, box_blur, fill_invalid, linear_to_srgb, save_png  # noqa: E402
from zones import read_zone_file  # noqa: E402

# Matériaux : couleur linéaire par défaut, métal, rugosité, variation de
# couleur et de rugosité, micro-relief (m), comportement à l'usure.
#   wear : "metal" = la peinture s'écaille sur du métal nu ; "polish" = le
#   métal s'éclaircit et se polit ; "light" = s'éclaircit ; None = aucune.
PRESETS = {
    "metal_peint":  dict(color=(0.20, 0.20, 0.22), metal=0.0, rough=0.42, cvar=0.10, rvar=0.10, bump=0.25, wear="metal"),
    "metal_nu":     dict(color=(0.55, 0.55, 0.56), metal=1.0, rough=0.35, cvar=0.08, rvar=0.14, bump=0.20, wear="polish"),
    "metal_sombre": dict(color=(0.09, 0.09, 0.10), metal=1.0, rough=0.38, cvar=0.08, rvar=0.12, bump=0.20, wear="polish"),
    "metal_brosse": dict(color=(0.60, 0.60, 0.61), metal=1.0, rough=0.30, cvar=0.05, rvar=0.10, bump=0.10, wear="polish", brushed=True),
    "chrome":       dict(color=(0.77, 0.78, 0.78), metal=1.0, rough=0.07, cvar=0.02, rvar=0.04, bump=0.05, wear="polish"),
    "or":           dict(color=(1.00, 0.77, 0.34), metal=1.0, rough=0.25, cvar=0.06, rvar=0.10, bump=0.10, wear="polish"),
    "cuivre":       dict(color=(0.95, 0.64, 0.54), metal=1.0, rough=0.30, cvar=0.10, rvar=0.12, bump=0.15, wear="polish"),
    "plastique":    dict(color=(0.20, 0.20, 0.20), metal=0.0, rough=0.50, cvar=0.05, rvar=0.08, bump=0.10, wear="light"),
    "caoutchouc":   dict(color=(0.03, 0.03, 0.03), metal=0.0, rough=0.85, cvar=0.05, rvar=0.06, bump=0.30, wear="light"),
    "pierre":       dict(color=(0.30, 0.28, 0.25), metal=0.0, rough=0.85, cvar=0.30, rvar=0.10, bump=1.00, wear="light"),
    "beton":        dict(color=(0.35, 0.35, 0.33), metal=0.0, rough=0.90, cvar=0.20, rvar=0.08, bump=0.70, wear="light"),
    "emissif":      dict(color=(0.03, 0.03, 0.03), metal=0.0, rough=0.30, cvar=0.02, rvar=0.05, bump=0.05, wear=None, emissive=True),
}
COLORED = {"metal_peint", "metal_sombre", "plastique", "caoutchouc", "pierre", "beton", "emissif"}
BARE_METAL = np.array([0.55, 0.55, 0.56], dtype=np.float32)
DIRT = np.array([0.050, 0.042, 0.033], dtype=np.float32)
DUST = np.array([0.32, 0.30, 0.27], dtype=np.float32)


def log(msg):
    print(f"[materials] {msg}", flush=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="materials", description="Composition des matériaux PBR.")
    p.add_argument("--mesh", required=True, help="Mesh de jeu avec ses UV (.glb, étape 2)")
    p.add_argument("--maps-dir", default=None, help="Dossier des cartes (défaut : celui du mesh)")
    p.add_argument("--name", default=None, help="Préfixe des cartes (défaut : nom du mesh sans « _uv »)")
    p.add_argument("--zones-file", default=None, help="Fichier des zones (défaut : <nom>_zones.txt)")
    p.add_argument("--wear", type=float, default=0.5, help="Usure des arêtes, 0 à 1 (défaut 0.5)")
    p.add_argument("--dirt", type=float, default=0.5, help="Saleté dans les creux, 0 à 1 (défaut 0.5)")
    p.add_argument("--dust", type=float, default=0.2, help="Poussière sur le dessus, 0 à 1 (défaut 0.2)")
    p.add_argument("--scale", type=float, default=1.0, help="Taille des motifs d'usure (défaut 1 ; 2 = deux fois plus gros)")
    p.add_argument("--noise-res", type=int, default=2048, help="Résolution du calcul des motifs (défaut 2048)")
    p.add_argument("--size", type=int, default=0, help="Résolution des textures (défaut : celle de la normal map)")
    p.add_argument("--seed", type=int, default=1, help="Graine des motifs d'usure")
    a = p.parse_args(argv)
    a.mesh = os.path.abspath(a.mesh)
    a.maps_dir = os.path.abspath(a.maps_dir or os.path.dirname(a.mesh))
    if a.name is None:
        stem = os.path.splitext(os.path.basename(a.mesh))[0]
        a.name = stem[:-3] if stem.endswith("_uv") else stem
    a.zones_file = a.zones_file or os.path.join(a.maps_dir, a.name + "_zones.txt")
    return a


# -- bruit 3D --------------------------------------------------------------------------
def _hash(ix, iy, iz, seed):
    h = (ix * 73856093) ^ (iy * 19349663) ^ (iz * 83492791) ^ (seed * 2654435761)
    h = h.astype(np.uint32)
    h ^= h >> np.uint32(13)
    h *= np.uint32(1274126177)
    h ^= h >> np.uint32(16)
    return (h & np.uint32(0xFFFFFF)).astype(np.float32) / float(0xFFFFFF)


def value_noise(p, seed):
    """Bruit de valeur 3D lissé (0-1)."""
    f = np.floor(p)
    t = p - f
    t = t * t * (3 - 2 * t)
    i = f.astype(np.int64)
    out = 0.0
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                w = (t[:, 0] if dx else 1 - t[:, 0]) * (t[:, 1] if dy else 1 - t[:, 1]) * (t[:, 2] if dz else 1 - t[:, 2])
                out = out + w * _hash(i[:, 0] + dx, i[:, 1] + dy, i[:, 2] + dz, seed)
    return out


def fbm(p, freq, octaves, seed):
    """Bruit fractal (somme d'octaves), normalisé autour de 0,5."""
    out, amp, tot = 0.0, 1.0, 0.0
    for o in range(octaves):
        out = out + amp * value_noise(p * freq * (2 ** o), seed + 17 * o)
        tot += amp
        amp *= 0.5
    return out / tot


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


# -- lecture des cartes ------------------------------------------------------------------
def read(path, size, mode="L"):
    im = Image.open(path)
    arr = np.asarray(im, dtype=np.float32)
    if im.mode.startswith("I") or arr.max() > 255:
        arr = arr / 65535.0
    else:
        arr = arr / 255.0
    if mode == "RGB" and arr.ndim == 2:
        arr = np.repeat(arr[..., None], 3, -1)
    if mode == "L" and arr.ndim == 3:
        arr = arr[..., 0]
    if arr.shape[0] != size:
        if arr.ndim == 2:
            arr = np.asarray(Image.fromarray(arr.astype(np.float32), "F").resize((size, size), Image.BILINEAR))
        else:
            arr = np.stack([np.asarray(Image.fromarray(arr[..., c].astype(np.float32), "F").resize((size, size), Image.BILINEAR))
                            for c in range(arr.shape[2])], -1)
    return arr


def read_normal16(path):
    """Normal map 16 bits RGB (PIL ne lit pas le RGB 16 bits : décodage direct)."""
    import struct
    import zlib

    with open(path, "rb") as fh:
        data = fh.read()
    off, idat = 8, b""
    w = h = bits = ctype = 0
    while off < len(data):
        n = struct.unpack(">I", data[off : off + 4])[0]
        tag = data[off + 4 : off + 8]
        body = data[off + 8 : off + 8 + n]
        if tag == b"IHDR":
            w, h, bits, ctype = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        off += 12 + n
    if bits != 16 or ctype != 2:
        return read(path, w, "RGB")
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(h, 1 + w * 6)
    if raw[:, 0].any():  # filtres PNG : on passe par PIL (8 bits)
        return read(path, w, "RGB")
    return raw[:, 1:].view(">u2").reshape(h, w, 3).astype(np.float32) / 65535.0


def resize(arr, size):
    if arr.shape[0] == size:
        return arr
    chans = [arr] if arr.ndim == 2 else [arr[..., c] for c in range(arr.shape[2])]
    out = [np.asarray(Image.fromarray(c.astype(np.float32), "F").resize((size, size), Image.BILINEAR)) for c in chans]
    return out[0] if arr.ndim == 2 else np.stack(out, -1)


# -- composition --------------------------------------------------------------------------
def run(a):
    t0 = time.time()
    base = os.path.join(a.maps_dir, a.name)
    log("Version du pipeline : " + pipeline_version())
    need = [f"{base}_normal.png", f"{base}_ao.png", f"{base}_curvature.png", f"{base}_zones_id.png", a.zones_file]
    missing = [p for p in need if not os.path.exists(p)]
    if missing:
        raise SystemExit("Fichiers manquants (étapes 3 et 5) : " + ", ".join(missing))
    zones = read_zone_file(a.zones_file)

    nrm = read_normal16(f"{base}_normal.png")
    size = a.size or nrm.shape[0]
    if nrm.shape[0] != size:
        nrm = resize(nrm, size)
    ao = read(f"{base}_ao.png", size)
    curv = read(f"{base}_curvature.png", size)
    height = read(f"{base}_height.png", size) if os.path.exists(f"{base}_height.png") else np.full((size, size), 0.5, np.float32)
    up = read(f"{base}_up.png", size) if os.path.exists(f"{base}_up.png") else np.full((size, size), 0.5, np.float32)
    zid_native = np.asarray(Image.open(f"{base}_zones_id.png").convert("L")).astype(np.int64)
    log(f"Cartes {size} px, {len(zones)} zones : " + ", ".join(f"{k}={v[0]}" for k, v in sorted(zones.items())))

    # Position et normale de la surface pour chaque pixel (motifs 3D).
    R = min(a.noise_res, size)
    tri_p, tri_n, tri_uv = cg.load_glb(a.mesh)
    pos, tnrm, covered = cg.texel_maps(tri_p, tri_n, tri_uv, R)
    pos = fill_invalid(pos, covered)
    sel = pos.reshape(-1, 3) / a.scale
    span = float(np.ptp(tri_p.reshape(-1, 3), 0).max())
    big = fbm(sel, 1.0 / (0.12 * span), 3, a.seed).reshape(R, R)      # taches
    mid = fbm(sel, 1.0 / (0.03 * span), 4, a.seed + 101).reshape(R, R)  # usure, rayures
    fine = fbm(sel, 1.0 / (0.006 * span), 3, a.seed + 202).reshape(R, R)  # grain
    brushed = fbm(sel * np.array([1.0, 1.0, 40.0]), 1.0 / (0.02 * span), 3, a.seed + 303).reshape(R, R)
    big, mid, fine, brushed = (resize(x, size) for x in (big, mid, fine, brushed))
    log(f"Motifs 3D calculés ({time.time() - t0:.0f} s)")

    # Matériau de base par zone, avec des transitions adoucies entre zones
    # (anticrénelage : la carte des zones est souvent moins fine que la texture).
    color = np.zeros((size, size, 3), np.float32)
    metal = np.zeros((size, size), np.float32)
    rough = np.zeros((size, size), np.float32)
    bump = np.zeros((size, size), np.float32)
    emis = np.zeros((size, size, 3), np.float32)
    wk = np.zeros((size, size, 4), np.float32)  # poids : rien, métal sous peinture, polissage, éclaircir
    kinds = {None: 0, "metal": 1, "polish": 2, "light": 3}
    ids = sorted(zones)
    weights = {}
    for z in ids:
        w = resize((zid_native == z).astype(np.float32), size)
        weights[z] = box_blur(w, max(1, size // 2048))
    total = sum(weights.values())
    any_zone = total > 0.5
    norm = np.where(total > 1e-6, 1.0 / np.maximum(total, 1e-6), 0.0)
    for z in ids:
        w = weights[z] * norm
        if not (w > 1e-3).any():
            continue
        mat, col = zones[z]
        pr = PRESETS[mat]
        c = np.array(pr["color"], np.float32)
        if col is not None and mat in COLORED | {"metal_nu", "metal_brosse", "chrome", "or", "cuivre"}:
            c = np.power(np.array(col, np.float32), 2.2)
        if pr.get("emissive"):
            emis += w[..., None] * np.power(np.array(col or (1.0, 1.0, 1.0), np.float32), 2.2)
            c = np.array(pr["color"], np.float32)
        var = 1 + pr["cvar"] * 2 * (big - 0.5) + pr["cvar"] * (fine - 0.5)
        color += w[..., None] * (c * var[..., None])
        metal += w * pr["metal"]
        r = pr["rough"] + pr["rvar"] * 2 * (mid - 0.5) + 0.05 * (fine - 0.5)
        if pr.get("brushed"):
            r = r + 0.12 * (brushed - 0.5)
        rough += w * r
        bump += w * pr["bump"]
        wk[..., kinds[pr["wear"]]] += w

    convex = np.clip((curv - 0.5) * 2, 0, 1)
    concave = np.clip((0.5 - curv) * 2, 0, 1)

    # 1) Arêtes usées.
    if a.wear > 0:
        t = 0.75 - 0.5 * a.wear
        edge = smoothstep(t, t + 0.15, convex * 1.4 + (mid - 0.5) * 0.9 + (fine - 0.5) * 0.3) * (1 - wk[..., 0])
        k = wk[..., 1] * edge  # peinture écaillée : métal nu
        color = color * (1 - k[..., None]) + BARE_METAL * k[..., None]
        metal = metal * (1 - k) + k
        rough = rough * (1 - k) + 0.28 * k
        k = wk[..., 2] * edge  # métal poli
        color = color * (1 + 0.6 * k[..., None])
        rough = rough * (1 - 0.45 * k)
        k = wk[..., 3] * edge * 0.6  # éclairci
        color = color * (1 + 0.5 * k[..., None])
        bump = bump + 0.3 * edge * wk[..., 1]  # bord de l'écaille

    # 2) Saleté dans les creux et en bas.
    if a.dirt > 0:
        d = (1 - ao) * 0.9 + concave * 0.8 + np.clip(0.25 - height, 0, 0.25) * 2.0
        d = d * (0.5 + big) + (mid - 0.5) * 0.4
        t = 0.9 - 0.6 * a.dirt
        dm = smoothstep(t, t + 0.35, d) * 0.85 * (1 - wk[..., 0])
        color = color * (1 - dm[..., None]) + DIRT * dm[..., None]
        metal = metal * (1 - dm)
        rough = rough * (1 - dm) + 0.85 * dm
        emis = emis * (1 - 0.5 * dm[..., None])

    # 3) Poussière sur le dessus.
    if a.dust > 0:
        u = np.clip((up - 0.6) / 0.4, 0, 1) ** 1.5
        dd = smoothstep(0.3, 0.8, u * (0.6 + big) * (0.7 + 0.6 * fine)) * a.dust * (1 - wk[..., 0])
        color = color * (1 - dd[..., None]) + DUST * dd[..., None]
        metal = metal * (1 - dd)
        rough = rough * (1 - dd) + 0.95 * dd

    rough = np.clip(rough, 0.03, 1.0)

    # 4) Normal map : relief du HighPoly + micro-relief des matériaux
    #    (combinaison « orientée », RNM), en convention DirectX à la sortie.
    texel_m = span / max(1.0, size * 0.5)  # ordre de grandeur d'un pixel en mètres
    h = (fine - 0.5) * 0.0004 * span * bump + (mid - 0.5) * 0.0004 * span * bump
    gx = (np.roll(h, -1, 1) - np.roll(h, 1, 1)) / (2 * texel_m)
    gy = (np.roll(h, -1, 0) - np.roll(h, 1, 0)) / (2 * texel_m)
    detail = np.stack([-gx, gy, np.ones_like(gx)], -1)  # OpenGL : +Y vers le haut de l'image
    detail /= np.linalg.norm(detail, axis=-1, keepdims=True)
    baked = nrm * 2 - 1
    baked[..., 1] *= -1  # DirectX -> OpenGL
    baked[..., 2] = np.maximum(baked[..., 2], 0.05)  # pas de normale tournée vers l'intérieur
    baked /= np.linalg.norm(baked, axis=-1, keepdims=True)
    t_ = baked + np.array([0, 0, 1.0], np.float32)
    u_ = detail * np.array([-1, -1, 1.0], np.float32)
    n = t_ * (t_ * u_).sum(-1, keepdims=True) / t_[..., 2:3] - u_
    n /= np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6)
    n[~any_zone] = baked[~any_zone]
    n[..., 1] *= -1  # -> DirectX (Unreal)

    out_dir = a.maps_dir
    name = a.name
    files = dict(BC=os.path.join(out_dir, f"T_{name}_BC.png"), N=os.path.join(out_dir, f"T_{name}_N.png"),
                 ORM=os.path.join(out_dir, f"T_{name}_ORM.png"))
    save_png(files["BC"], linear_to_srgb(color)[::-1])
    save_png(files["N"], (n * 0.5 + 0.5)[::-1], 16)
    save_png(files["ORM"], np.stack([ao, rough, metal], -1)[::-1])
    if emis.max() > 0:
        files["E"] = os.path.join(out_dir, f"T_{name}_E.png")
        save_png(files["E"], linear_to_srgb(emis)[::-1])
    report = dict(textures=files, size=size, zones={str(k): v[0] for k, v in zones.items()},
                  wear=a.wear, dirt=a.dirt, dust=a.dust, seconds=round(time.time() - t0, 1))
    with open(os.path.join(out_dir, f"{name}_materials_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    for k, v in files.items():
        log(f"  {k:<4}-> {os.path.basename(v)}")
    log(f"Terminé en {time.time() - t0:.0f} s")
    return report


def main(argv=None):
    return run(parse_args(argv))


if __name__ == "__main__":
    main()
