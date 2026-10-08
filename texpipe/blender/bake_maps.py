"""
Étape 3 du pipeline : calcul (« bake ») des cartes de géométrie.

Projette le mesh détaillé (HighPoly Tripo) sur le mesh de jeu déplié à
l'étape 2 et produit, sur ses UV :

    <nom>_normal.png          normal map tangente (DirectX pour Unreal, 16 bits)
    <nom>_ao.png              occlusion ambiante
    <nom>_curvature.png       courbure (0,5 = plat, clair = arête vive, sombre = creux)
    <nom>_height.png          hauteur dans l'objet (0 = bas, 1 = haut)
    <nom>_up.png              orientation vers le haut (1 = face au ciel)
    <nom>_basecolor_high.png  couleur Tripo du HighPoly (guide pour les zones)
    <nom>_roughness_high.png  rugosité Tripo   (idem, si le HighPoly en a une)
    <nom>_metallic_high.png   métal Tripo      (idem)
    <nom>_bake_report.json    mesures et contrôles

Meshes symétriques : si des parties du mesh partagent les mêmes UV (moitié
recréée en miroir à l'étape 2, ou îlots empilés volontairement), une seule
copie est calculée ; les autres réutilisent ses pixels. La détection est
automatique : un mesh sans UV partagées est calculé en entier.

Usage :
    blender -b --factory-startup -P bake_maps.py -- \
        --low robot_uv.glb --high robot_high.glb --texture-size 4096
"""

import argparse
import json
import math
import os
import sys
import time
import zlib
from collections import defaultdict

import bpy  # doit précéder bmesh quand bpy est utilisé comme module Python
import bmesh
import numpy as np
from mathutils.bvhtree import BVHTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uv_optimize import raster_count, setup_cycles  # noqa: E402

ALL_MAPS = ("normal", "ao", "curvature", "height", "up", "basecolor", "roughness", "metallic")


def log(msg):
    print(f"[bake_maps] {msg}", flush=True)


def parse_args(argv):
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    p = argparse.ArgumentParser(prog="bake_maps", description="Calcul des cartes de géométrie depuis le HighPoly.")
    p.add_argument("--low", required=True, help="Mesh de jeu avec ses UV (sortie de l'étape 2)")
    p.add_argument("--high", required=True, help="Mesh détaillé (HighPoly Tripo)")
    p.add_argument("--output-dir", default=None, help="Dossier de sortie (défaut : celui du mesh de jeu)")
    p.add_argument("--name", default=None, help="Préfixe des fichiers (défaut : nom du mesh de jeu sans « _uv »)")
    p.add_argument("--texture-size", type=int, default=4096, help="Résolution des cartes (défaut 4096)")
    p.add_argument("--maps", default=",".join(ALL_MAPS), help="Cartes à produire, séparées par des virgules (défaut : toutes)")
    p.add_argument("--normal-format", default="directx", choices=["directx", "opengl"],
                   help="Convention de la normal map : directx (défaut, Unreal) ou opengl (Blender, Unity)")
    p.add_argument("--cage", default="auto",
                   help="Distance de projection en mètres, ou « auto » (défaut : mesurée entre les deux meshes, "
                        "agrandie si des pixels ne trouvent pas le HighPoly)")
    p.add_argument("--ao-distance", default="auto", help="Portée de l'occlusion en mètres (défaut : 5 %% de la taille de l'objet)")
    p.add_argument("--ao-samples", type=int, default=128, help="Échantillons de l'occlusion (défaut 128)")
    p.add_argument("--samples", type=int, default=8, help="Échantillons (anticrénelage) des autres cartes (défaut 8)")
    p.add_argument("--margin", type=int, default=None, help="Débord autour des îlots en pixels (défaut : taille/128)")
    p.add_argument("--mirror", default="auto", choices=["auto", "off"],
                   help="auto (défaut) : les parties aux UV partagées ne sont calculées qu'une fois ; off : tout est calculé")
    p.add_argument("--bake-side", default="+X", choices=["+X", "-X"],
                   help="Côté calculé quand deux parties partagent les mêmes UV (défaut +X)")
    p.add_argument("--high-normal-map", action="store_true",
                   help="Inclure la normal map Tripo du HighPoly dans la normal map (désactivé : souvent bruitée)")
    p.add_argument("--preview", default=None,
                   help="Image de contrôle : rendu du mesh de jeu avec ses cartes à côté du HighPoly (face et 3/4)")
    p.add_argument("--cpu", action="store_true", help="Calcul sur le processeur même si une carte graphique est disponible")
    a = p.parse_args(argv)
    a.low = os.path.abspath(a.low)
    a.high = os.path.abspath(a.high)
    a.output_dir = os.path.abspath(a.output_dir or os.path.dirname(a.low))
    if a.name is None:
        stem = os.path.splitext(os.path.basename(a.low))[0]
        a.name = stem[:-3] if stem.endswith("_uv") else stem
    a.maps = [m.strip() for m in a.maps.split(",") if m.strip()]
    bad = [m for m in a.maps if m not in ALL_MAPS]
    if bad:
        raise SystemExit(f"Cartes inconnues : {bad} (possibles : {', '.join(ALL_MAPS)})")
    if a.margin is None:
        a.margin = max(4, a.texture_size // 128)
    return a


# -- import --------------------------------------------------------------------
def import_objects(path):
    """Importe un fichier et renvoie un seul objet mesh (parties fusionnées,
    transformations appliquées)."""
    before = set(bpy.data.objects)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise SystemExit(f"Format non supporté : {ext}")
    meshes = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    if not meshes:
        raise SystemExit(f"Aucun mesh dans {path}")
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    world = obj.matrix_world.copy()
    obj.parent = None
    obj.matrix_world = world
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return obj


def world_points(obj):
    co = np.empty(len(obj.data.vertices) * 3)
    obj.data.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


# -- UV partagées (symétrie) ------------------------------------------------------
def uv_islands(bm, uvl, plane_x=None):
    """Îlots UV : faces reliées par une arête dont les UV sont continues.
    Avec `plane_x`, un îlot n'enjambe pas le plan x = plane_x (la moitié
    recréée en miroir touche l'originale au plan, avec les mêmes UV)."""
    parent = list(range(len(bm.faces)))
    side = [f.calc_center_median().x > plane_x if plane_x is not None else True for f in bm.faces]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in bm.edges:
        if len(e.link_faces) != 2:
            continue
        f1, f2 = e.link_faces
        if side[f1.index] != side[f2.index]:
            continue
        l1 = next(l for l in f1.loops if l.edge == e)
        l2 = next(l for l in f2.loops if l.edge == e)
        # Même arête parcourue en sens inverse dans la face voisine.
        if (l1[uvl].uv - l2.link_loop_next[uvl].uv).length < 1e-6 and \
           (l1.link_loop_next[uvl].uv - l2[uvl].uv).length < 1e-6:
            parent[find(f1.index)] = find(f2.index)
    groups = defaultdict(list)
    for f in bm.faces:
        groups[find(f.index)].append(f)
    return list(groups.values())


def set_aside_shared_uv(obj, side):
    """Les îlots dont les UV sont identiques à celles d'un autre (copie en
    miroir) sont décalés d'une case hors de la texture pendant le calcul :
    seule la copie du côté `side` est calculée, les autres réutiliseront ses
    pixels dans le moteur (MikkTSpace gère le reflet). Renvoie le nombre de
    faces mises de côté."""
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    uvl = bm.loops.layers.uv.active
    sign = 1.0 if side == "+X" else -1.0
    xs = [v.co.x for v in bm.verts]
    plane = 0.5 * (min(xs) + max(xs))
    by_sig = defaultdict(list)
    for isl in uv_islands(bm, uvl, plane):
        sig = frozenset((round(l[uvl].uv.x, 4), round(l[uvl].uv.y, 4)) for f in isl for l in f.loops)
        cx = sum(f.calc_center_median().x for f in isl) / len(isl) - plane
        by_sig[sig].append((sign * cx, isl))
    moved = 0
    for copies in by_sig.values():
        if len(copies) < 2:
            continue
        copies.sort(key=lambda c: -c[0])
        for _, isl in copies[1:]:
            for f in isl:
                for l in f.loops:
                    l[uvl].uv.x += 1.0
                moved += 1
    bm.to_mesh(me)
    bm.free()
    return moved


def uv_triangles(obj):
    """Triangles UV (dans la case 0-1 seulement), en coordonnées 0-1."""
    me = obj.data
    me.calc_loop_triangles()
    uv = np.empty(len(me.loops) * 2)
    me.uv_layers.active.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    tl = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("loops", tl)
    tri = uv[tl.reshape(-1, 3)]
    inside = (tri.min((1, 2)) >= -1e-6) & (tri.max((1, 2)) <= 1 + 1e-6)
    return tri[inside]


# -- distance de projection --------------------------------------------------------
def auto_cage(low, high, diag):
    """Distance de projection : écart mesuré entre les deux surfaces (dans
    les deux sens), avec une marge."""
    dg = bpy.context.evaluated_depsgraph_get()
    bvh_high = BVHTree.FromObject(high, dg)
    bvh_low = BVHTree.FromObject(low, dg)
    d1 = []
    for v in low.data.vertices:
        hit = bvh_high.find_nearest(v.co)
        if hit[0] is not None:
            d1.append(hit[3])
    hp = world_points(high)
    rng = np.random.default_rng(0)
    sample = hp[rng.choice(len(hp), size=min(len(hp), 100000), replace=False)]
    d2 = []
    for p in sample:
        hit = bvh_low.find_nearest(p)
        if hit[0] is not None:
            d2.append(hit[3])
    d1 = np.array(d1) if d1 else np.zeros(1)
    d2 = np.array(d2) if d2 else np.zeros(1)
    gap = max(np.percentile(d1, 99), np.percentile(d2, 98))
    cage = float(np.clip(gap * 1.25 + 0.002 * diag, 0.003 * diag, 0.05 * diag))
    return cage, float(np.percentile(d1, 99)), float(np.percentile(d2, 98))


# -- matériaux -------------------------------------------------------------------
def new_material(name):
    mat = bpy.data.materials.new(name)
    if bpy.app.version < (5, 0, 0):  # toujours actif à partir de Blender 5
        mat.use_nodes = True
    mat.node_tree.nodes.clear()
    return mat


def set_materials(obj, mats_per_slot):
    me = obj.data
    for i, m in enumerate(mats_per_slot):
        if i < len(me.materials):
            me.materials[i] = m
        else:
            me.materials.append(m)


def principled(mat):
    if not mat or not mat.node_tree:
        return None
    return next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)


def socket_emission_material(src, socket_name):
    """Copie du matériau `src` qui émet la valeur branchée sur l'entrée
    `socket_name` de son Principled BSDF (texture ou valeur fixe). Renvoie
    (matériau, True si une texture y est branchée)."""
    if src is None or principled(src) is None:
        mat = new_material("bake_const")
        nt = mat.node_tree
        emit = nt.nodes.new("ShaderNodeEmission")
        emit.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
        return mat, False
    mat = src.copy()
    nt = mat.node_tree
    bsdf = principled(mat)
    sock = bsdf.inputs[socket_name]
    emit = nt.nodes.new("ShaderNodeEmission")
    out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
    if out is None:
        out = nt.nodes.new("ShaderNodeOutputMaterial")
    linked = sock.is_linked
    if linked:
        nt.links.new(sock.links[0].from_socket, emit.inputs["Color"])
    else:
        v = sock.default_value
        emit.inputs["Color"].default_value = tuple(v) if hasattr(v, "__len__") else (v, v, v, 1.0)
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return mat, linked


def plain_material():
    mat = new_material("bake_plain")
    nt = mat.node_tree
    bsdf = nt.nodes.new("ShaderNodeBsdfDiffuse")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat


def geometry_material(zmin, zmax):
    """Émet R = orientation vers le haut, G = hauteur, B = 1 (sert à repérer
    les pixels qui n'ont pas trouvé le HighPoly)."""
    mat = new_material("bake_geometry")
    nt = mat.node_tree
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    sep_n = nt.nodes.new("ShaderNodeSeparateXYZ")
    sep_p = nt.nodes.new("ShaderNodeSeparateXYZ")
    up = nt.nodes.new("ShaderNodeMath")
    up.operation = "MULTIPLY_ADD"
    up.inputs[1].default_value = 0.5
    up.inputs[2].default_value = 0.5
    hgt = nt.nodes.new("ShaderNodeMapRange")
    hgt.inputs["From Min"].default_value = zmin
    hgt.inputs["From Max"].default_value = zmax
    comb = nt.nodes.new("ShaderNodeCombineXYZ")
    comb.inputs["Z"].default_value = 1.0
    emit = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(geo.outputs["Normal"], sep_n.inputs["Vector"])
    nt.links.new(geo.outputs["Position"], sep_p.inputs["Vector"])
    nt.links.new(sep_n.outputs["Z"], up.inputs[0])
    nt.links.new(sep_p.outputs["Z"], hgt.inputs["Value"])
    nt.links.new(up.outputs["Value"], comb.inputs["X"])
    nt.links.new(hgt.outputs["Result"], comb.inputs["Y"])
    nt.links.new(comb.outputs["Vector"], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return mat


# -- calcul --------------------------------------------------------------------
class Baker:
    def __init__(self, low, high, args):
        self.low, self.high, self.a = low, high, args
        size = args.texture_size
        self.image = bpy.data.images.new("bake_target", width=size, height=size, alpha=False, float_buffer=True)
        self.image.colorspace_settings.name = "Non-Color"
        mat = new_material("bake_low")
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = self.image
        mat.node_tree.nodes.active = node
        me = low.data
        me.materials.clear()
        me.materials.append(mat)
        for p in me.polygons:
            p.material_index = 0
        # Le mesh de jeu ne doit jamais masquer le HighPoly (occlusion).
        for attr in ("visible_camera", "visible_diffuse", "visible_glossy", "visible_transmission",
                     "visible_volume_scatter", "visible_shadow"):
            if hasattr(low, attr):
                setattr(low, attr, False)
        self.high_mats = [s.material for s in high.material_slots] or [None]
        self.scene = bpy.context.scene
        self.device = "CPU"
        self.times = {}

    def bake(self, label, kind, cage, samples, high_mats=None, **kw):
        a = self.a
        set_materials(self.high, high_mats if high_mats is not None else self.high_mats)
        if a.cpu:
            self.scene.render.engine = "CYCLES"
            self.scene.cycles.device = "CPU"
            self.scene.cycles.samples = samples
            self.device = "CPU"
        else:
            self.device = setup_cycles(self.scene, samples)
        bpy.ops.object.select_all(action="DESELECT")
        self.high.select_set(True)
        self.low.select_set(True)
        bpy.context.view_layer.objects.active = self.low
        t = time.time()
        bpy.ops.object.bake(type=kind, use_selected_to_active=True, cage_extrusion=cage,
                            max_ray_distance=2.0 * cage, margin=a.margin, margin_type="EXTEND",
                            use_clear=True, target="IMAGE_TEXTURES", **kw)
        size = a.texture_size
        px = np.empty(size * size * 4, dtype=np.float32)
        self.image.pixels.foreach_get(px)
        dt = time.time() - t
        self.times[label] = round(self.times.get(label, 0) + dt, 1)
        return px.reshape(size, size, 4)[..., :3]  # ligne 0 = bas de la texture (v = 0)


# -- post-traitements ----------------------------------------------------------------
def box_blur(img, r):
    if r <= 0:
        return img
    out = img
    for axis in (0, 1):
        pad = [(0, 0)] * img.ndim
        pad[axis] = (r + 1, r)
        c = np.cumsum(np.pad(out, pad, mode="edge"), axis=axis, dtype=np.float64)
        n = out.shape[axis]
        hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
        lo = np.take(c, np.arange(0, n), axis=axis)
        out = ((hi - lo) / (2 * r + 1)).astype(np.float32)
    return out


def curvature_from_normal(nrm_gl, mask, size):
    """Courbure à partir de la normal map tangente (convention OpenGL) :
    divergence de la normale, sur plusieurs échelles (arêtes fines et formes
    plus larges). 0,5 = plat, > 0,5 convexe (arêtes), < 0,5 concave."""
    nx = nrm_gl[..., 0] * 2 - 1
    ny = nrm_gl[..., 1] * 2 - 1
    acc = np.zeros(nx.shape, dtype=np.float32)
    base = max(1, size // 2048)
    for k, s in enumerate((base, 2 * base, 4 * base, 8 * base)):
        bx = box_blur(nx, s // 2)
        by = box_blur(ny, s // 2)
        div = (np.roll(bx, -s, 1) - np.roll(bx, s, 1) + np.roll(by, -s, 0) - np.roll(by, s, 0)) / (2.0 * s)
        div *= s ** 0.5  # les grandes échelles ont des pentes plus douces
        ref = np.percentile(np.abs(div[mask]), 99) if mask.any() else 1.0
        acc += div / max(ref, 1e-6) * (1.0 / (k + 1))
    acc /= sum(1.0 / (k + 1) for k in range(4))
    return np.clip(0.5 + 0.5 * acc, 0, 1)


def fill_invalid(img, valid):
    """Remplit les pixels non valides à partir des pixels valides voisins
    (pyramide « push-pull ») : zones où le HighPoly n'a pas été trouvé et
    débord autour des îlots."""
    squeeze = img.ndim == 2
    x = (img[..., None] if squeeze else img).astype(np.float32)
    v = valid.astype(np.float32)
    if v.all() or not v.any():
        return img
    pyr = []
    cx, cv = x * v[..., None], v
    while True:
        pyr.append((cx, cv))
        h, w = cv.shape
        if h <= 1 or w <= 1:
            break
        h2, w2 = h - h % 2, w - w % 2
        cx = cx[:h2:2, :w2:2] + cx[1:h2:2, :w2:2] + cx[:h2:2, 1:w2:2] + cx[1:h2:2, 1:w2:2]
        cv = cv[:h2:2, :w2:2] + cv[1:h2:2, :w2:2] + cv[:h2:2, 1:w2:2] + cv[1:h2:2, 1:w2:2]
    sx, sv = pyr[-1]
    res = sx / np.maximum(sv, 1e-8)[..., None]
    for sx, sv in reversed(pyr[:-1]):
        h, w = sv.shape
        up = np.repeat(np.repeat(res, 2, 0), 2, 1)
        up = np.pad(up, ((0, max(0, h - up.shape[0])), (0, max(0, w - up.shape[1])), (0, 0)), mode="edge")[:h, :w]
        up = box_blur(up, 1)
        res = np.where((sv > 0)[..., None], sx / np.maximum(sv, 1e-8)[..., None], up)
    out = np.where(valid[..., None], x, res)
    return out[..., 0] if squeeze else out


def flatten_missing_normals(nrm, hit, feather):
    """Normale neutre (forme du mesh de jeu) là où le HighPoly n'a pas été
    trouvé, avec un fondu de `feather` pixels."""
    w = np.clip(box_blur(hit.astype(np.float32), feather), 0, 1) * hit
    v = nrm * 2 - 1
    v = v * w[..., None] + np.array([0.0, 0.0, 1.0], dtype=np.float32) * (1 - w[..., None])
    v /= np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-6)
    return v * 0.5 + 0.5


def linear_to_srgb(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def save_png(path, img, bits=8):
    """PNG 8 ou 16 bits, gris ou RGB, à partir d'un tableau 0-1 dont la
    ligne 0 est le bas de la texture (convention Blender)."""
    img = np.clip(np.asarray(img, dtype=np.float32), 0, 1)[::-1]
    h, w = img.shape[:2]
    ch = 1 if img.ndim == 2 else img.shape[2]
    if bits == 16:
        data = np.round(img * 65535).astype(">u2")
    else:
        data = np.round(img * 255).astype(np.uint8)
    raw = data.reshape(h, w * ch)
    raw = np.concatenate([np.zeros((h, 1), dtype=raw.dtype), raw], 1) if bits == 8 else \
        np.concatenate([np.zeros((h, 1), dtype=np.uint8), raw.view(np.uint8).reshape(h, -1)], 1)
    color_type = 0 if ch == 1 else 2

    def chunk(tag, payload):
        c = tag + payload
        return len(payload).to_bytes(4, "big") + c + (zlib.crc32(c) & 0xFFFFFFFF).to_bytes(4, "big")

    ihdr = w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([bits, color_type, 0, 0, 0])
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", zlib.compress(raw.astype(np.uint8).tobytes(), 6)))
        fh.write(chunk(b"IEND", b""))


def render_preview(low, high, maps, path, args, diag):
    """Rendus côte à côte : mesh de jeu avec normal map et AO, puis HighPoly,
    de face et de 3/4. Les deux doivent se ressembler."""
    scene = bpy.context.scene
    if args.cpu:
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = 32
    else:
        setup_cycles(scene, 32)
    scene.render.resolution_x = scene.render.resolution_y = 768
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.view_transform = "Standard"
    world = scene.world
    if bpy.app.version < (5, 0, 0):
        world.use_nodes = True
    bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs["Color"].default_value = (0.05, 0.05, 0.06, 1.0)
    bg.inputs["Strength"].default_value = 3.0
    sun = bpy.data.objects.new("preview_sun", bpy.data.lights.new("preview_sun", "SUN"))
    sun.data.energy = 3.5
    sun.rotation_euler = (math.radians(50), 0.0, math.radians(30))
    scene.collection.objects.link(sun)
    cam = bpy.data.objects.new("preview_cam", bpy.data.cameras.new("preview_cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    pts = world_points(low)
    lo, hi = pts.min(0), pts.max(0)
    center = (lo + hi) / 2
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = float(max(hi[0] - lo[0], hi[2] - lo[2], hi[1] - lo[1])) * 1.1
    cam.data.clip_end = 100 * diag

    def tex(nt, key):
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = bpy.data.images.load(maps[key])
        node.image.colorspace_settings.name = "Non-Color"
        return node

    mat = new_material("preview_low")
    nt = mat.node_tree
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Roughness"].default_value = 0.5
    outn = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(bsdf.outputs["BSDF"], outn.inputs["Surface"])
    if "normal" in maps:
        nm = tex(nt, "normal")
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        if args.normal_format == "directx":  # Blender lit en convention OpenGL
            sep = nt.nodes.new("ShaderNodeSeparateColor")
            inv = nt.nodes.new("ShaderNodeMath")
            inv.operation = "SUBTRACT"
            inv.inputs[0].default_value = 1.0
            comb = nt.nodes.new("ShaderNodeCombineColor")
            nt.links.new(nm.outputs["Color"], sep.inputs["Color"])
            nt.links.new(sep.outputs[0], comb.inputs[0])
            nt.links.new(sep.outputs[1], inv.inputs[1])
            nt.links.new(inv.outputs[0], comb.inputs[1])
            nt.links.new(sep.outputs[2], comb.inputs[2])
            nt.links.new(comb.outputs["Color"], nmap.inputs["Color"])
        else:
            nt.links.new(nm.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    if "ao" in maps:
        ao = tex(nt, "ao")
        mul = nt.nodes.new("ShaderNodeMath")
        mul.operation = "MULTIPLY"
        mul.inputs[1].default_value = 0.6
        nt.links.new(ao.outputs["Color"], mul.inputs[0])
        nt.links.new(mul.outputs["Value"], bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = (0.6, 0.6, 0.6, 1.0)
    gray = new_material("preview_high")
    g = gray.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    g.inputs["Base Color"].default_value = (0.6, 0.6, 0.6, 1.0)
    g.inputs["Roughness"].default_value = 0.5
    go = gray.node_tree.nodes.new("ShaderNodeOutputMaterial")
    gray.node_tree.links.new(g.outputs["BSDF"], go.inputs["Surface"])
    set_materials(low, [mat] * max(1, len(low.material_slots)))
    set_materials(high, [gray] * max(1, len(high.material_slots)))
    for attr in ("visible_camera", "visible_diffuse", "visible_glossy", "visible_transmission",
                 "visible_volume_scatter", "visible_shadow"):
        if hasattr(low, attr):
            setattr(low, attr, True)

    panels = []
    tmp = path + ".tmp.png"
    for angle in (0.0, 45.0):
        a = math.radians(angle)
        cam.location = (center[0] + math.sin(a) * 3 * diag, center[1] - math.cos(a) * 3 * diag, center[2])
        cam.rotation_euler = (math.radians(90), 0.0, a)
        for obj, other in ((low, high), (high, low)):
            obj.hide_render, other.hide_render = False, True
            scene.render.filepath = tmp
            bpy.ops.render.render(write_still=True)
            im = bpy.data.images.load(tmp)
            px = np.empty(im.size[0] * im.size[1] * 4, dtype=np.float32)
            im.pixels.foreach_get(px)
            panels.append(px.reshape(im.size[1], im.size[0], 4)[..., :3])
            bpy.data.images.remove(im)
    os.remove(tmp)
    save_png(path, np.concatenate(panels, 1))
    log(f"Aperçu : {path} (mesh de jeu puis HighPoly, de face puis de 3/4)")


# -- programme principal -----------------------------------------------------------------
def run(a):
    t0 = time.time()
    os.makedirs(a.output_dir, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    low = import_objects(a.low)
    low.name = "low"
    if not low.data.uv_layers:
        raise SystemExit("Le mesh de jeu n'a pas d'UV : lancer d'abord l'étape 2 (uv_optimize).")
    high = import_objects(a.high)
    high.name = "high"
    pl, ph = world_points(low), world_points(high)
    diag = float(np.linalg.norm(pl.max(0) - pl.min(0)))
    offset = float(np.linalg.norm((ph.max(0) + ph.min(0)) / 2 - (pl.max(0) + pl.min(0)) / 2))
    log(f"Mesh de jeu : {len(low.data.polygons)} faces ; HighPoly : {len(high.data.polygons)} faces")
    if offset > 0.02 * diag:
        log(f"ATTENTION : les deux meshes ne sont pas superposés (écart {offset:.3f} m). "
            "Ils doivent venir de la même génération Tripo.")

    moved = set_aside_shared_uv(low, a.bake_side) if a.mirror == "auto" else 0
    if moved:
        log(f"UV partagées (symétrie) : {moved} faces réutilisent les pixels de leur double, côté {a.bake_side} calculé")
    else:
        log("Aucune UV partagée : tout le mesh est calculé")
    size = a.texture_size
    tri = uv_triangles(low)
    cover = raster_count(tri * size, size)
    mask = cover > 0
    overlap = float((cover > 1).sum()) / max(1, mask.sum())
    if overlap > 0.001:
        log(f"ATTENTION : {100 * overlap:.2f} % des pixels sont couverts par plusieurs faces (UV qui se chevauchent)")
    # Intérieur des îlots (sans le bord anticrénelé), pour les contrôles.
    inner = mask & np.roll(mask, 1, 0) & np.roll(mask, -1, 0) & np.roll(mask, 1, 1) & np.roll(mask, -1, 1)

    if a.ao_distance == "auto":
        ao_dist = 0.05 * diag
    else:
        ao_dist = float(a.ao_distance)
    if bpy.context.scene.world is None:
        bpy.context.scene.world = bpy.data.worlds.new("World")
    bpy.context.scene.world.light_settings.distance = ao_dist

    baker = Baker(low, high, a)
    report = {"low": a.low, "high": a.high, "texture_size": size, "faces_sharing_uv": moved,
              "uv_overlap_percent": round(100 * overlap, 3), "maps": {}}

    # 1) Passe géométrique (rapide) : hauteur, orientation, et repérage des
    #    pixels qui ne trouvent pas le HighPoly -> réglage de la projection.
    zmin, zmax = float(ph[:, 2].min()), float(ph[:, 2].max())
    geo_mat = geometry_material(zmin, zmax)
    if a.cage == "auto":
        cage, d_low, d_high = auto_cage(low, high, diag)
        log(f"Écart entre les surfaces : {1000 * d_low:.1f} mm (mesh de jeu -> HighPoly), "
            f"{1000 * d_high:.1f} mm (HighPoly -> mesh de jeu)")
    else:
        cage = float(a.cage)
    # Une projection plus longue rattrape les parties éloignées, mais risque
    # d'attraper une autre surface : on ne l'allonge que si cela réduit
    # nettement les pixels sans correspondance.
    tried = []
    for attempt in range(4):
        geo = baker.bake("geometry", "EMIT", cage, max(1, min(a.samples, 4)), high_mats=[geo_mat] * len(baker.high_mats))
        miss = float((geo[..., 2] < 0.5)[inner].mean()) if inner.any() else 0.0
        log(f"Projection {1000 * cage:.1f} mm : {100 * miss:.2f} % des pixels sans correspondance")
        tried.append((cage, miss, geo))
        if a.cage != "auto" or miss < 0.005:
            break
        if len(tried) > 1 and miss > 0.75 * tried[-2][1]:
            cage, miss, geo = tried[-2]  # gain trop faible : on garde la précédente
            break
        cage *= 1.6
    check = np.where(mask, 0.35, 0.0)[..., None].repeat(3, 2)
    check[inner & (geo[..., 2] < 0.5)] = (1.0, 0.1, 0.1)
    save_png(os.path.join(a.output_dir, a.name) + "_bake_check.png", check)
    report["cage_m"] = round(cage, 5)
    hit = geo[..., 2] >= 0.5  # pixels (débord compris) qui ont trouvé le HighPoly
    report["pixels_missed_percent"] = round(100 * miss, 3)
    if miss > 0.005:
        log("Note : des zones ne trouvent pas le HighPoly (parties absentes ou trop éloignées) ; "
            "la normal map y garde la forme du mesh de jeu, les autres cartes sont complétées "
            "par les pixels voisins (zones en rouge dans _bake_check.png).")
    base = os.path.join(a.output_dir, a.name)

    def out(kind, img, bits=8):
        path = f"{base}_{kind}.png"
        save_png(path, img, bits)
        report["maps"][kind] = path
        log(f"  -> {os.path.basename(path)}")

    geo = fill_invalid(geo, hit)
    if "height" in a.maps:
        out("height", geo[..., 1], 16)
    if "up" in a.maps:
        out("up", geo[..., 0])

    # 2) Normal map (et courbure, qui en dérive).
    if "normal" in a.maps or "curvature" in a.maps:
        mats = baker.high_mats if a.high_normal_map else [plain_material()] * len(baker.high_mats)
        nrm = baker.bake("normal", "NORMAL", cage, a.samples, high_mats=mats, normal_space="TANGENT",
                         normal_r="POS_X", normal_g="POS_Y", normal_b="POS_Z")
        nrm = flatten_missing_normals(nrm, hit, max(2, size // 512))
        if "normal" in a.maps:
            n_out = nrm.copy()
            if a.normal_format == "directx":
                n_out[..., 1] = 1.0 - n_out[..., 1]
            out("normal", n_out, 16)
            report["normal_format"] = a.normal_format
        if "curvature" in a.maps:
            out("curvature", curvature_from_normal(nrm, inner, size))

    # 3) Occlusion ambiante.
    if "ao" in a.maps:
        ao = fill_invalid(baker.bake("ao", "AO", cage, a.ao_samples, high_mats=[plain_material()] * len(baker.high_mats)), hit)
        out("ao", ao[..., 0])
        report["ao_distance_m"] = round(ao_dist, 4)
        report["ao_mean"] = round(float(ao[..., 0][inner].mean()), 3) if inner.any() else None

    # 4) Couleur, rugosité et métal de Tripo (guides pour le découpage en zones).
    for kind, sock, srgb in (("basecolor", "Base Color", True), ("roughness", "Roughness", False),
                             ("metallic", "Metallic", False)):
        if kind not in a.maps:
            continue
        pairs = [socket_emission_material(m, sock) for m in baker.high_mats]
        if not any(linked for _, linked in pairs) and kind != "basecolor":
            log(f"  (pas de texture de {kind} sur le HighPoly : carte ignorée)")
            continue
        img = fill_invalid(baker.bake(kind, "EMIT", cage, max(1, min(a.samples, 4)), high_mats=[m for m, _ in pairs]), hit)
        if srgb:
            out(f"{kind}_high", linear_to_srgb(img))
        else:
            out(f"{kind}_high", img[..., 0])

    if a.preview:
        render_preview(low, high, report["maps"], os.path.abspath(a.preview), a, diag)
        report["preview"] = os.path.abspath(a.preview)
    report["device"] = baker.device
    report["bake_seconds"] = baker.times
    report["total_seconds"] = round(time.time() - t0, 1)
    with open(f"{base}_bake_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    log(f"Terminé en {time.time() - t0:.0f} s ({baker.device})")
    return report


def main(argv=None):
    return run(parse_args(sys.argv if argv is None else argv))


if __name__ == "__main__":
    main()
