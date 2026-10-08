"""
Étape 4 du pipeline : couleur guide par IA (MV-Adapter).

À partir du mesh de jeu déplié (étape 2) et de l'image de référence de face,
MV-Adapter génère 6 vues cohérentes de l'objet (face, droite, dos, gauche,
dessus, dessous), guidées par la forme exacte du mesh. Les vues sont ensuite
reprojetées sur les UV : on obtient une texture couleur alignée sur le mesh,
qui sert de guide aux étapes suivantes (découpage en zones, matériaux).

Tout le rendu géométrique (vues de guidage, reprojection) est fait ici en
numpy : pas de nvdiffrast (licence non commerciale).

Produit :
    <nom>_color_guide.png     texture couleur sur les UV du mesh de jeu
    <nom>_views.png           les 6 vues générées (réutilisables : --views)
    <nom>_control.png         vues de guidage (position, normales) pour contrôle
    <nom>_color_report.json   mesures

Usage (Python du pipeline, .venv) :
    python color_guide.py --mesh robot_uv.glb --image robot_front.png
"""

import argparse
import json
import math
import os
import struct
import sys
import time

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common"))
from imgops import box_blur, fill_invalid, save_png  # noqa: E402

# Caméras de MV-Adapter (scripts/inference_ig2mv_*.py) : 4 vues autour,
# dessus et dessous, orthographiques.
AZIMUTHS = [x - 90 for x in [0, 90, 180, 270, 180, 180]]
ELEVATIONS = [0, 0, 0, 0, 89.99, -89.99]
VIEW_NAMES = ["face", "droite", "dos", "gauche", "dessus", "dessous"]
VIEW_WEIGHTS = [1.0, 1.0, 1.0, 1.0, 0.5, 0.5]  # dessus / dessous moins fiables
ORTHO = 0.55
DISTANCE = 1.8

VARIANTS = {
    # Tient dans 8 Go de mémoire graphique.
    "sd21": dict(base=["stabilityai/stable-diffusion-2-1-base", "Manojb/stable-diffusion-2-1-base"],
                 vae=None, weight="mvadapter_ig2mv_sd21.safetensors", size=512),
    # Meilleure qualité, mais 8 Go est juste : déchargement partiel sur le processeur.
    "sdxl": dict(base=["stabilityai/stable-diffusion-xl-base-1.0"], vae="madebyollin/sdxl-vae-fp16-fix",
                 weight="mvadapter_ig2mv_sdxl.safetensors", size=768),
}


def log(msg):
    print(f"[color_guide] {msg}", flush=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="color_guide", description="Couleur guide par IA (MV-Adapter).")
    p.add_argument("--mesh", required=True, help="Mesh de jeu avec ses UV (.glb, sortie de l'étape 2)")
    p.add_argument("--image", default=None, help="Image de référence de face (fond uni ou transparent)")
    p.add_argument("--views", default=None, help="Réutiliser des vues déjà générées (<nom>_views.png, éventuellement retouchées) au lieu de lancer l'IA")
    p.add_argument("--output-dir", default=None, help="Dossier de sortie (défaut : celui du mesh)")
    p.add_argument("--name", default=None, help="Préfixe des fichiers (défaut : nom du mesh sans « _uv »)")
    p.add_argument("--texture-size", type=int, default=2048, help="Résolution de la texture guide (défaut 2048)")
    p.add_argument("--variant", default="sd21", choices=sorted(VARIANTS), help="Modèle : sd21 (défaut, 8 Go) ou sdxl (plus fin, plus lent)")
    p.add_argument("--prompt", default="", help="Description facultative (en anglais), ex. « dark metal robot, purple glowing lines »")
    p.add_argument("--negative-prompt", default="watermark, ugly, deformed, noisy, blurry, low contrast")
    p.add_argument("--steps", type=int, default=50, help="Étapes de diffusion (défaut 50)")
    p.add_argument("--guidance", type=float, default=3.0, help="Force du texte (défaut 3.0)")
    p.add_argument("--reference-scale", type=float, default=1.0, help="Force de l'image de référence (défaut 1.0)")
    p.add_argument("--seed", type=int, default=42, help="Graine (même graine = même résultat ; changer pour une autre proposition)")
    p.add_argument("--bg-tolerance", type=int, default=12, help="Détourage du fond de la référence : tolérance par pas (défaut 12)")
    p.add_argument("--base-model", default=None, help="Dépôt Hugging Face du modèle de base (remplace celui de la variante)")
    a = p.parse_args(argv)
    if not a.image and not a.views:
        p.error("--image (référence de face) ou --views (vues déjà générées) est nécessaire")
    a.mesh = os.path.abspath(a.mesh)
    a.output_dir = os.path.abspath(a.output_dir or os.path.dirname(a.mesh))
    if a.name is None:
        stem = os.path.splitext(os.path.basename(a.mesh))[0]
        a.name = stem[:-3] if stem.endswith("_uv") else stem
    return a


# -- lecture du glTF (sans dépendance) --------------------------------------------------
_COMP = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _node_matrix(node):
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
    t = np.array(node.get("translation", [0, 0, 0]), dtype=np.float64)
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    s = np.array(node.get("scale", [1, 1, 1]), dtype=np.float64)
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = r * s[None, :]
    m[:3, 3] = t
    return m


def load_glb(path):
    """Triangles du fichier (toutes les primitives, transformations des
    nœuds appliquées) : positions, normales et UV par coin de triangle,
    dans le repère de Blender (Z vers le haut), comme MV-Adapter."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"glTF":
        raise SystemExit("Seul le format .glb est accepté ici (sortie de l'étape 2).")
    off, js, binc = 12, None, b""
    while off < len(data):
        n, kind = struct.unpack_from("<I4s", data, off)
        chunk = data[off + 8 : off + 8 + n]
        if kind == b"JSON":
            js = json.loads(chunk)
        elif kind == b"BIN\x00":
            binc = chunk
        off += 8 + n

    def accessor(i):
        acc = js["accessors"][i]
        bv = js["bufferViews"][acc["bufferView"]]
        dt = np.dtype(_COMP[acc["componentType"]])
        nc = _NCOMP[acc["type"]]
        start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        stride = bv.get("byteStride", 0) or dt.itemsize * nc
        raw = np.frombuffer(binc, dtype=np.uint8, count=stride * (acc["count"] - 1) + dt.itemsize * nc, offset=start)
        rows = np.lib.stride_tricks.as_strided(raw, shape=(acc["count"], dt.itemsize * nc), strides=(stride, 1))
        arr = np.ascontiguousarray(rows).view(dt).reshape(acc["count"], nc).astype(np.float64)
        if acc.get("normalized"):
            arr /= np.iinfo(dt).max
        return arr

    pos, nrm, uv = [], [], []
    scene = js["scenes"][js.get("scene", 0)]
    stack = [(n, np.eye(4)) for n in scene["nodes"]]
    while stack:
        ni, parent = stack.pop()
        node = js["nodes"][ni]
        m = parent @ _node_matrix(node)
        stack.extend((c, m) for c in node.get("children", []))
        if "mesh" not in node:
            continue
        for prim in js["meshes"][node["mesh"]]["primitives"]:
            if prim.get("mode", 4) != 4 or "TEXCOORD_0" not in prim["attributes"]:
                continue
            P = accessor(prim["attributes"]["POSITION"])
            idx = accessor(prim["indices"]).astype(np.int64).ravel() if "indices" in prim else np.arange(len(P))
            T = idx.reshape(-1, 3)
            P = P @ m[:3, :3].T + m[:3, 3]
            if "NORMAL" in prim["attributes"]:
                N = accessor(prim["attributes"]["NORMAL"]) @ np.linalg.inv(m[:3, :3])
            else:
                N = None
            UV = accessor(prim["attributes"]["TEXCOORD_0"])
            pos.append(P[T])
            nrm.append(N[T] if N is not None else None)
            uv.append(UV[T])
    if not pos:
        raise SystemExit("Aucun triangle avec UV dans le mesh : lancer d'abord l'étape 2.")
    tri_p = np.concatenate(pos)
    if all(n is not None for n in nrm):
        tri_n = np.concatenate(nrm)
    else:
        tri_n = smooth_normals(tri_p)
    tri_uv = np.concatenate(uv)
    # glTF (Y vers le haut) -> Blender / MV-Adapter (Z vers le haut).
    swap = lambda a: np.stack([a[..., 0], -a[..., 2], a[..., 1]], -1)
    tri_p, tri_n = swap(tri_p), swap(tri_n)
    tri_n /= np.maximum(np.linalg.norm(tri_n, axis=-1, keepdims=True), 1e-12)
    return tri_p, tri_n, tri_uv


def smooth_normals(tri_p):
    flat = tri_p.reshape(-1, 3)
    span = float(np.ptp(flat, 0).max()) or 1.0
    key = np.round(flat / (span * 1e-6)).astype(np.int64)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    fn = np.cross(tri_p[:, 1] - tri_p[:, 0], tri_p[:, 2] - tri_p[:, 0])
    acc = np.zeros((inv.max() + 1, 3))
    np.add.at(acc, inv, np.repeat(fn, 3, 0))
    n = acc[inv].reshape(-1, 3, 3)
    return n / np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-12)


# -- rastérisation (numpy) --------------------------------------------------------------
def rasterize(xy, depth, H, W):
    """Rastérise des triangles (`xy` : (T, 3, 2) en pixels, x = colonne,
    y = ligne ; `depth` : (T, 3), le plus grand gagne). Renvoie, par pixel,
    l'indice du triangle (-1 = vide) et les coordonnées barycentriques."""
    T = len(xy)
    lo = np.floor(xy.min(1) - 0.5).astype(int)
    hi = np.ceil(xy.max(1) - 0.5).astype(int) + 1
    lo = np.maximum(lo, 0)
    hi = np.minimum(hi, [W, H])
    size = hi - lo
    a, b, c = xy[:, 0], xy[:, 1], xy[:, 2]
    det = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
    ok = (size > 0).all(1) & (np.abs(det) > 1e-12)
    pix_l, z_l, tri_l, l1_l, l2_l = [], [], [], [], []
    idx = np.nonzero(ok)[0]
    bw = 1 << np.ceil(np.log2(np.maximum(size[idx, 0], 1))).astype(int)
    bh = 1 << np.ceil(np.log2(np.maximum(size[idx, 1], 1))).astype(int)
    for key in set(zip(bw.tolist(), bh.tolist())):
        sel = idx[(bw == key[0]) & (bh == key[1])]
        gw, gh = key
        step = max(1, 2_000_000 // (gw * gh))
        for s in range(0, len(sel), step):
            t = sel[s : s + step]
            xs = lo[t, 0, None, None] + np.arange(gw)[None, None, :] + 0.5
            ys = lo[t, 1, None, None] + np.arange(gh)[None, :, None] + 0.5
            ax, ay = a[t, 0, None, None], a[t, 1, None, None]
            bx, by = b[t, 0, None, None], b[t, 1, None, None]
            cx, cy = c[t, 0, None, None], c[t, 1, None, None]
            d = det[t, None, None]
            l0 = ((by - cy) * (xs - cx) + (cx - bx) * (ys - cy)) / d
            l1 = ((cy - ay) * (xs - cx) + (ax - cx) * (ys - cy)) / d
            l2 = 1 - l0 - l1
            inside = (l0 >= -1e-6) & (l1 >= -1e-6) & (l2 >= -1e-6) & (xs < W) & (ys < H)
            ti, yy, xx = np.nonzero(inside)
            px = (xs[ti, 0, xx] - 0.5).astype(np.int64)
            py = (ys[ti, yy, 0] - 0.5).astype(np.int64)
            tri = t[ti]
            L0, L1, L2 = l0[ti, yy, xx], l1[ti, yy, xx], l2[ti, yy, xx]
            z = L0 * depth[tri, 0] + L1 * depth[tri, 1] + L2 * depth[tri, 2]
            pix_l.append(py * W + px)
            z_l.append(z)
            tri_l.append(tri)
            l1_l.append(L1)
            l2_l.append(L2)
    tri_id = np.full(H * W, -1, dtype=np.int64)
    bary = np.zeros((H * W, 2), dtype=np.float32)
    zbuf = np.full(H * W, -np.inf, dtype=np.float32)
    if pix_l:
        pix = np.concatenate(pix_l)
        z = np.concatenate(z_l)
        order = np.lexsort((-z, pix))
        pix_s = pix[order]
        first = order[np.r_[True, pix_s[1:] != pix_s[:-1]]]
        p = pix[first]
        tri_id[p] = np.concatenate(tri_l)[first]
        bary[p, 0] = np.concatenate(l1_l)[first]
        bary[p, 1] = np.concatenate(l2_l)[first]
        zbuf[p] = z[first]
    return tri_id.reshape(H, W), bary.reshape(H, W, 2), zbuf.reshape(H, W)


def interpolate(attr, tri_id, bary):
    """Attribut par coin (T, 3, C) interpolé aux pixels."""
    t = np.maximum(tri_id, 0)
    l1, l2 = bary[..., 0:1], bary[..., 1:2]
    out = (1 - l1 - l2) * attr[t, 0] + l1 * attr[t, 1] + l2 * attr[t, 2]
    out[tri_id < 0] = 0
    return out


# -- caméras de MV-Adapter --------------------------------------------------------------
def cameras():
    """(position de la caméra, droite, haut, direction vers la caméra) par
    vue, comme mvadapter.utils.mesh_utils.camera.get_c2w."""
    out = []
    for az, el in zip(AZIMUTHS, ELEVATIONS):
        a, e = math.radians(az), math.radians(el)
        pos = DISTANCE * np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
        look = -pos / np.linalg.norm(pos)
        right = np.cross(look, [0.0, 0.0, 1.0])
        right /= np.linalg.norm(right)
        up = np.cross(right, look)
        up /= np.linalg.norm(up)
        out.append((pos, right, up, -look))
    return out


def project(p, cam, size):
    """Points (…, 3) -> (colonne, ligne) en pixels et profondeur (plus
    grand = plus près de la caméra)."""
    _, right, up, to_cam = cam
    col = (p @ right + ORTHO) / (2 * ORTHO) * size
    row = (ORTHO - p @ up) / (2 * ORTHO) * size
    return np.stack([col, row], -1), p @ to_cam


def render_views(tri_p, tri_n, size):
    """Vues de guidage de MV-Adapter : position (+0,5) et normale (/2 + 0,5),
    fond à 0,5 ; plus le tampon de profondeur pour la reprojection."""
    views = []
    for cam in cameras():
        xy, depth = project(tri_p, cam, size)
        tri_id, bary, zbuf = rasterize(xy, depth, size, size)
        pos = interpolate(tri_p, tri_id, bary)
        nrm = interpolate(tri_n, tri_id, bary)
        nrm /= np.maximum(np.linalg.norm(nrm, axis=-1, keepdims=True), 1e-12)
        nrm[tri_id < 0] = 0
        views.append(dict(pos=np.clip(pos + 0.5, 0, 1), normal=np.clip(nrm / 2 + 0.5, 0, 1),
                          mask=tri_id >= 0, zbuf=zbuf, cam=cam))
    return views


# -- image de référence -------------------------------------------------------------
def remove_background(img, tol):
    """Fond uni (blanc, gris...) retiré par remplissage depuis les bords :
    un pixel est du fond s'il touche le fond et lui ressemble à `tol` près
    (pas à pas : suit les dégradés et les ombres douces)."""
    rgb = np.asarray(img.convert("RGB"), dtype=np.int16)
    H, W, _ = rgb.shape
    bg = np.zeros((H, W), dtype=bool)
    bg[0, :] = bg[-1, :] = bg[:, 0] = bg[:, -1] = True
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    ref = np.median(border, 0)
    bg &= np.abs(rgb - ref).max(-1) < 4 * tol
    shifts = ((1, 0), (-1, 0), (0, 1), (0, -1))
    similar = [np.abs(rgb - np.roll(rgb, d, (0, 1))).max(-1) <= tol for d in shifts]
    while True:
        grown = bg.copy()
        for _ in range(8):  # plusieurs pas avant de tester la convergence
            for d, sim in zip(shifts, similar):
                grown |= np.roll(grown, d, (0, 1)) & sim
        if (grown == bg).all():
            break
        bg = grown
    alpha = (~bg).astype(np.float32)
    alpha = np.clip(box_blur(alpha, 1) * 1.5 - 0.25, 0, 1)  # bord adouci
    out = np.dstack([rgb.astype(np.uint8), (alpha * 255).astype(np.uint8)])
    return Image.fromarray(out, "RGBA")


def preprocess_reference(image, size):
    """Comme MV-Adapter : objet recadré, plus grand côté à 90 %, centré sur
    fond gris moyen."""
    image = np.array(image)
    alpha = image[..., 3] > 0
    ys, xs = np.where(alpha)
    y0, y1 = max(ys.min() - 1, 0), min(ys.max() + 1, alpha.shape[0])
    x0, x1 = max(xs.min() - 1, 0), min(xs.max() + 1, alpha.shape[1])
    crop = image[y0:y1, x0:x1]
    h, w = crop.shape[:2]
    if h > w:
        w, h = int(w * size * 0.9 / h), int(size * 0.9)
    else:
        h, w = int(h * size * 0.9 / w), int(size * 0.9)
    crop = np.array(Image.fromarray(crop).resize((w, h), Image.LANCZOS)).astype(np.float32) / 255
    out = np.full((size, size, 3), 0.5, dtype=np.float32)
    sy, sx = (size - h) // 2, (size - w) // 2
    out[sy : sy + h, sx : sx + w] = crop[..., :3] * crop[..., 3:4] + 0.5 * (1 - crop[..., 3:4])
    return Image.fromarray((out * 255).round().astype(np.uint8))


# -- génération (MV-Adapter) -----------------------------------------------------------------
def generate(a, views, reference):
    import torch

    from mvadapter.models.attention_processor import DecoupledMVRowColSelfAttnProcessor2_0
    from mvadapter.schedulers.scheduling_shift_snr import ShiftSNRScheduler

    v = VARIANTS[a.variant]
    if a.variant == "sdxl":
        from mvadapter.pipelines.pipeline_mvadapter_i2mv_sdxl import MVAdapterI2MVSDXLPipeline as Pipe
    else:
        from mvadapter.pipelines.pipeline_mvadapter_i2mv_sd import MVAdapterI2MVSDPipeline as Pipe
    if not torch.cuda.is_available():
        raise SystemExit("Aucune carte graphique NVIDIA détectée par PyTorch.")
    device, dtype = "cuda", torch.float16
    kw = {}
    if v["vae"]:
        from diffusers import AutoencoderKL

        kw["vae"] = AutoencoderKL.from_pretrained(v["vae"], torch_dtype=dtype)
    pipe, err = None, None
    for base in ([a.base_model] if a.base_model else v["base"]):
        try:
            pipe = Pipe.from_pretrained(base, torch_dtype=dtype, use_safetensors=True, **kw)
            log(f"Modèle de base : {base}")
            break
        except Exception as e:  # dépôt retiré de Hugging Face, réseau...
            err = e
            log(f"  {base} indisponible ({type(e).__name__}), essai suivant")
    if pipe is None:
        raise SystemExit(f"Modèle de base introuvable : {err}")
    pipe.scheduler = ShiftSNRScheduler.from_scheduler(pipe.scheduler, shift_mode="interpolated", shift_scale=8.0)
    pipe.init_custom_adapter(num_views=6, self_attn_processor=DecoupledMVRowColSelfAttnProcessor2_0)
    pipe.load_custom_adapter("huanngzh/mv-adapter", weight_name=v["weight"])
    pipe.to(dtype=dtype)  # l'adaptateur est créé en float32
    if a.variant == "sdxl":
        pipe.enable_model_cpu_offload()  # 8 Go : seule la partie active est sur la carte
    else:
        pipe.to(device=device)
    pipe.cond_encoder.to(device=device, dtype=dtype)
    pipe.enable_vae_slicing()

    control = np.stack([np.concatenate([vw["pos"], vw["normal"]], -1) for vw in views])
    control = torch.from_numpy(control).permute(0, 3, 1, 2).to(device=device, dtype=dtype)
    size = v["size"]
    prompt = "high quality" + (", " + a.prompt if a.prompt else "")
    gen = torch.Generator(device=device).manual_seed(a.seed)
    images = pipe(prompt, height=size, width=size, num_inference_steps=a.steps, guidance_scale=a.guidance,
                  num_images_per_prompt=6, control_image=control, control_conditioning_scale=1.0,
                  reference_image=reference, reference_conditioning_scale=a.reference_scale,
                  negative_prompt=a.negative_prompt, generator=gen).images
    del pipe
    torch.cuda.empty_cache()
    return [np.asarray(im.convert("RGB"), dtype=np.float32) / 255 for im in images]


# -- reprojection sur les UV -------------------------------------------------------------
def bilinear(img, xy):
    H, W = img.shape[:2]
    x = np.clip(xy[..., 0] - 0.5, 0, W - 1.001)
    y = np.clip(xy[..., 1] - 0.5, 0, H - 1.001)
    x0, y0 = x.astype(int), y.astype(int)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    return (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x0 + 1] * fx * (1 - fy)
            + img[y0 + 1, x0] * (1 - fx) * fy + img[y0 + 1, x0 + 1] * fx * fy)


def texel_maps(tri_p, tri_n, tri_uv, size):
    """Position et normale de la surface pour chaque pixel de la texture.
    Si des faces partagent les mêmes UV (miroir de l'étape 2), le côté +X
    l'emporte, comme au calcul des cartes."""
    xy = np.stack([tri_uv[..., 0] * size, tri_uv[..., 1] * size], -1)  # UV glTF : v vers le bas
    prio = np.repeat(tri_p[:, :, 0].mean(1, keepdims=True), 3, 1)
    tri_id, bary, _ = rasterize(xy, prio, size, size)
    pos = interpolate(tri_p, tri_id, bary)
    nrm = interpolate(tri_n, tri_id, bary)
    nrm /= np.maximum(np.linalg.norm(nrm, axis=-1, keepdims=True), 1e-12)
    return pos, nrm, tri_id >= 0


def erode(mask, r):
    out = mask.copy()
    for _ in range(r):
        p = np.pad(out, 1, constant_values=False)
        out = p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return out


def back_project(images, views, pos, nrm, covered):
    """Couleur de chaque pixel de texture : moyenne des vues qui le voient,
    pondérée par l'angle (une vue de face compte plus qu'une vue rasante)."""
    size_v = views[0]["mask"].shape[0]
    acc = np.zeros(pos.shape[:2] + (3,), dtype=np.float32)
    wsum = np.zeros(pos.shape[:2], dtype=np.float32)
    eps = 2.5 * (2 * ORTHO / size_v)  # tolérance de profondeur : ~2 pixels
    sel = np.nonzero(covered)
    p, n = pos[sel], nrm[sel]
    for img, vw, vweight in zip(images, views, VIEW_WEIGHTS):
        if img.shape[0] != size_v:
            img = np.asarray(Image.fromarray((img * 255).astype(np.uint8)).resize((size_v, size_v)), np.float32) / 255
        xy, depth = project(p, vw["cam"], size_v)
        col = np.clip(xy[:, 0].astype(int), 0, size_v - 1)
        row = np.clip(xy[:, 1].astype(int), 0, size_v - 1)
        inner = erode(vw["mask"], 1)  # pas les bords de silhouette (fond mélangé)
        visible = inner[row, col] & (depth >= vw["zbuf"][row, col] - eps)
        cos = np.clip(n @ vw["cam"][3], 0, 1)
        w = vweight * cos ** 2 * visible
        c = bilinear(img, xy)
        acc[sel] += c * w[:, None]
        wsum[sel] += w
    seen = wsum > 1e-4
    color = acc / np.maximum(wsum, 1e-8)[..., None]
    return color, seen


# -- programme principal ----------------------------------------------------------------
def run(a):
    t0 = time.time()
    os.makedirs(a.output_dir, exist_ok=True)
    base = os.path.join(a.output_dir, a.name)
    tri_p, tri_n, tri_uv = load_glb(a.mesh)
    log(f"Mesh : {len(tri_p)} triangles")
    # Normalisation de MV-Adapter (objet centré, plus grande coordonnée à 0,5).
    flat = tri_p.reshape(-1, 3)
    center = (flat.min(0) + flat.max(0)) / 2
    scale = 0.5 / np.abs(flat - center).max()
    tri_p = (tri_p - center) * scale

    size_v = VARIANTS[a.variant]["size"]
    views = render_views(tri_p, tri_n, size_v)
    ctrl = np.concatenate([np.concatenate([v["pos"] for v in views], 1),
                           np.concatenate([v["normal"] for v in views], 1)], 0)
    save_png(f"{base}_control.png", ctrl[::-1])  # save_png attend la ligne 0 en bas
    log(f"Vues de guidage : {base}_control.png ({time.time() - t0:.0f} s)")

    if a.views:
        grid = np.asarray(Image.open(a.views).convert("RGB"), dtype=np.float32) / 255
        h = grid.shape[0]
        images = [grid[:, i * h : (i + 1) * h] for i in range(6)]
        log(f"Vues reprises de {a.views}")
    else:
        ref = Image.open(a.image)
        ref = ref if ref.mode == "RGBA" else remove_background(ref, a.bg_tolerance)
        reference = preprocess_reference(ref, size_v)
        reference.save(f"{base}_reference.png")
        t = time.time()
        images = generate(a, views, reference)
        log(f"Vues générées ({a.variant}, {time.time() - t:.0f} s)")
        save_png(f"{base}_views.png", np.concatenate(images, 1)[::-1])

    size = a.texture_size
    pos, nrm, covered = texel_maps(tri_p, tri_n, tri_uv, size)
    color, seen = back_project(images, views, pos, nrm, covered)
    seen_frac = float(seen[covered].mean()) if covered.any() else 0.0
    color = fill_invalid(color, seen)
    save_png(f"{base}_color_guide.png", color[::-1])
    log(f"Texture guide : {base}_color_guide.png ({100 * seen_frac:.1f} % de la surface vue par au moins une vue)")
    report = dict(mesh=a.mesh, image=a.image, views=a.views or f"{base}_views.png", variant=a.variant,
                  seed=a.seed, prompt=a.prompt, texture_size=size, seen_percent=round(100 * seen_frac, 2),
                  color_guide=f"{base}_color_guide.png", seconds=round(time.time() - t0, 1))
    with open(f"{base}_color_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    log(f"Terminé en {time.time() - t0:.0f} s")
    return report


def main(argv=None):
    return run(parse_args(argv))


if __name__ == "__main__":
    main()
