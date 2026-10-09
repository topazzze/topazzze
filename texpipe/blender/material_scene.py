"""
Scène Blender des matériaux photo (AmbientCG / Poly Haven) appliqués par zone.

Deux modes :
    --candidates : pour chaque zone, rend l'objet avec chacun de ses matériaux
                   candidats (images <nom>_renders/zoneN_K.png, assemblées et
                   légendées ensuite par find_materials.py --sheets) ;
    --build      : construit la scène finale avec les matériaux retenus dans
                   <nom>_materials_choice.txt et l'enregistre en .blend
                   (nœuds éditables : un groupe par zone, usure, saleté, LED).

Les textures photo sont plaquées en projection « boîte » (coordonnées de
l'objet, taille réelle du motif) : pas de dépendance aux UV ni de couture.
Le relief du HighPoly (normal map de l'étape 3) est conservé, l'usure des
arêtes et la saleté des creux suivent la courbure et l'occlusion calculées.

    blender -b --factory-startup -P material_scene.py -- --mesh robot_uv.glb --candidates
"""

import argparse
import json
import math
import os
import re
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bake_maps import import_objects, new_material, world_points  # noqa: E402
from uv_optimize import setup_cycles  # noqa: E402


def log(msg):
    print(f"[material_scene] {msg}", flush=True)


def parse_args(argv):
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    p = argparse.ArgumentParser(prog="material_scene")
    p.add_argument("--mesh", required=True)
    p.add_argument("--maps-dir", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--candidates", action="store_true", help="Rendre les candidats de chaque zone")
    p.add_argument("--build", action="store_true", help="Construire et enregistrer la scène finale (.blend)")
    p.add_argument("--wear", type=float, default=0.5)
    p.add_argument("--dirt", type=float, default=0.5)
    p.add_argument("--emission", type=float, default=3.0, help="Intensité des LED (défaut 3)")
    p.add_argument("--size", type=int, default=640, help="Taille des rendus des candidats")
    p.add_argument("--samples", type=int, default=48)
    p.add_argument("--pack", action="store_true", help="Embarquer les textures dans le .blend (fichier autonome)")
    p.add_argument("--cpu", action="store_true")
    a = p.parse_args(argv)
    a.mesh = os.path.abspath(a.mesh)
    a.maps_dir = os.path.abspath(a.maps_dir or os.path.dirname(a.mesh))
    if a.name is None:
        stem = os.path.splitext(os.path.basename(a.mesh))[0]
        a.name = stem[:-3] if stem.endswith("_uv") else stem
    return a


# -- fichiers du pipeline ------------------------------------------------------------------
def read_zones(path):
    """{zone: (matériau, (r, g, b) ou None)} depuis <nom>_zones.txt."""
    out = {}
    for line in open(path, encoding="utf-8"):
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        zid, rest = line.split("=", 1)
        words = rest.split()
        col = None
        if len(words) > 1 and re.fullmatch(r"#[0-9a-fA-F]{6}", words[1]):
            col = tuple(int(words[1][i : i + 2], 16) / 255 for i in (1, 3, 5))
        out[int(zid)] = (words[0], col)
    return out


def read_choice(path):
    """{zone: (« source:id » | « procedural » | « emissif », taille en m ou None)}."""
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        zid, rest = line.split("=", 1)
        words = rest.split()
        size = None
        for w in words[1:]:
            if w.startswith("taille="):
                size = float(w.split("=", 1)[1])
        out[int(zid)] = (words[0] if words else "procedural", size)
    return out


# -- nœuds ---------------------------------------------------------------------------------
def load_image(path, color=False):
    im = bpy.data.images.load(os.path.abspath(path), check_existing=True)
    im.colorspace_settings.name = "sRGB" if color else "Non-Color"
    return im


def pbr_group(name, maps, tile_m, fallback_color):
    """Groupe de nœuds d'un matériau photo : sorties Couleur, Rugosité, Métal,
    Hauteur, en projection boîte (coordonnées objet, motif de `tile_m` m)."""
    g = bpy.data.node_groups.new(name, "ShaderNodeTree")
    if hasattr(g, "interface"):
        for n, t in (("Color", "NodeSocketColor"), ("Roughness", "NodeSocketFloat"),
                     ("Metallic", "NodeSocketFloat"), ("Height", "NodeSocketFloat")):
            g.interface.new_socket(n, in_out="OUTPUT", socket_type=t)
    out = g.nodes.new("NodeGroupOutput")
    tc = g.nodes.new("ShaderNodeTexCoord")
    mp = g.nodes.new("ShaderNodeMapping")
    s = 1.0 / max(tile_m, 1e-3)
    mp.inputs["Scale"].default_value = (s, s, s)
    g.links.new(tc.outputs["Object"], mp.inputs["Vector"])

    def tex(key, color=False):
        if key not in maps:
            return None
        n = g.nodes.new("ShaderNodeTexImage")
        n.image = load_image(maps[key], color)
        n.projection = "BOX"
        n.projection_blend = 0.25
        g.links.new(mp.outputs["Vector"], n.inputs["Vector"])
        return n

    col = tex("color", True)
    if col:
        g.links.new(col.outputs["Color"], out.inputs["Color"])
    else:
        rgb = g.nodes.new("ShaderNodeRGB")
        rgb.outputs[0].default_value = (*fallback_color, 1.0)
        g.links.new(rgb.outputs[0], out.inputs["Color"])
    for key, sock, default in (("roughness", "Roughness", 0.5), ("metalness", "Metallic", 0.0), ("height", "Height", 0.5)):
        t = tex(key)
        if t:
            g.links.new(t.outputs["Color"], out.inputs[sock])
        else:
            v = g.nodes.new("ShaderNodeValue")
            v.outputs[0].default_value = default
            g.links.new(v.outputs[0], out.inputs[sock])
    return g


PROCEDURAL = {  # repli quand une zone garde le matériau calculé : (couleur, rugosité, métal)
    "metal_sombre": ((0.09, 0.09, 0.10), 0.38, 1.0), "metal_peint": ((0.2, 0.2, 0.22), 0.42, 0.0),
    "metal_nu": ((0.55, 0.55, 0.56), 0.35, 1.0), "metal_brosse": ((0.6, 0.6, 0.61), 0.3, 1.0),
    "chrome": ((0.77, 0.78, 0.78), 0.07, 1.0), "or": ((1.0, 0.77, 0.34), 0.25, 1.0),
    "cuivre": ((0.95, 0.64, 0.54), 0.3, 1.0), "plastique": ((0.2, 0.2, 0.2), 0.5, 0.0),
    "caoutchouc": ((0.03, 0.03, 0.03), 0.85, 0.0), "pierre": ((0.3, 0.28, 0.25), 0.85, 0.0),
    "beton": ((0.35, 0.35, 0.33), 0.9, 0.0),
}


def procedural_group(name, mat, col):
    c, r, m = PROCEDURAL.get(mat, ((0.5, 0.5, 0.5), 0.5, 0.0))
    if col is not None:
        c = tuple(x ** 2.2 for x in col)
    g = bpy.data.node_groups.new(name, "ShaderNodeTree")
    if hasattr(g, "interface"):
        for n, t in (("Color", "NodeSocketColor"), ("Roughness", "NodeSocketFloat"),
                     ("Metallic", "NodeSocketFloat"), ("Height", "NodeSocketFloat")):
            g.interface.new_socket(n, in_out="OUTPUT", socket_type=t)
    out = g.nodes.new("NodeGroupOutput")
    rgb = g.nodes.new("ShaderNodeRGB")
    rgb.outputs[0].default_value = (*c, 1.0)
    g.links.new(rgb.outputs[0], out.inputs["Color"])
    for sock, v in (("Roughness", r), ("Metallic", m), ("Height", 0.5)):
        n = g.nodes.new("ShaderNodeValue")
        n.outputs[0].default_value = v
        g.links.new(n.outputs[0], out.inputs[sock])
    return g


class ZoneMaterial:
    """Matériau final : un nœud de groupe par zone, mélangés selon la carte
    des zones (UV), + normal map calculée, usure, saleté, LED."""

    def __init__(self, a, zones, initial):
        base = os.path.join(a.maps_dir, a.name)
        self.mat = new_material(f"M_{a.name}_zones")
        nt = self.nt = self.mat.node_tree
        L = nt.links
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        bsdf = self.bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        L.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        uvmap = nt.nodes.new("ShaderNodeUVMap")

        def uv_tex(path, closest=False):
            n = nt.nodes.new("ShaderNodeTexImage")
            n.image = load_image(path)
            if closest:
                n.interpolation = "Closest"
            L.new(uvmap.outputs["UV"], n.inputs["Vector"])
            return n

        zid = uv_tex(base + "_zones_id.png", closest=True)
        to255 = nt.nodes.new("ShaderNodeMath")
        to255.operation = "MULTIPLY"
        to255.inputs[1].default_value = 255.0
        L.new(zid.outputs["Color"], to255.inputs[0])
        self.groups = {}
        color = rough = metal = height = None
        emis_mask = None
        for z, (mat, col) in sorted(zones.items()):
            mask = nt.nodes.new("ShaderNodeMath")
            mask.operation = "COMPARE"
            mask.inputs[1].default_value = float(z)
            mask.inputs[2].default_value = 0.5
            L.new(to255.outputs["Value"], mask.inputs[0])
            if mat == "emissif":
                emis_mask = (mask, col or (1.0, 1.0, 1.0))
                continue
            node = nt.nodes.new("ShaderNodeGroup")
            node.node_tree = initial(z)
            node.label = f"Zone {z} ({mat})"
            self.groups[z] = node
            if color is None:
                color, rough, metal, height = (node.outputs["Color"], node.outputs["Roughness"],
                                               node.outputs["Metallic"], node.outputs["Height"])
                continue
            mc = nt.nodes.new("ShaderNodeMix")
            mc.data_type = "RGBA"
            L.new(mask.outputs["Value"], mc.inputs["Factor"])
            L.new(color, mc.inputs[6])
            L.new(node.outputs["Color"], mc.inputs[7])
            color = mc.outputs[2]
            mixed = []
            for prev, sock in ((rough, "Roughness"), (metal, "Metallic"), (height, "Height")):
                mf = nt.nodes.new("ShaderNodeMix")
                mf.data_type = "FLOAT"
                L.new(mask.outputs["Value"], mf.inputs["Factor"])
                L.new(prev, mf.inputs[2])
                L.new(node.outputs[sock], mf.inputs[3])
                mixed.append(mf.outputs[0])
            rough, metal, height = mixed

        # Usure des arêtes (courbure) et saleté (occlusion) des cartes de l'étape 3.
        curv = uv_tex(base + "_curvature.png") if os.path.exists(base + "_curvature.png") else None
        ao = uv_tex(base + "_ao.png") if os.path.exists(base + "_ao.png") else None
        if curv is not None and color is not None and a.wear > 0:
            ramp = nt.nodes.new("ShaderNodeValToRGB")
            lo = 0.62 - 0.1 * a.wear
            ramp.color_ramp.elements[0].position = lo
            ramp.color_ramp.elements[1].position = lo + 0.08
            L.new(curv.outputs["Color"], ramp.inputs["Fac"])
            light = nt.nodes.new("ShaderNodeMix")
            light.data_type = "RGBA"
            light.blend_type = "SCREEN"
            light.inputs[7].default_value = (0.25, 0.25, 0.26, 1.0)
            L.new(ramp.outputs["Color"], light.inputs["Factor"])
            L.new(color, light.inputs[6])
            color = light.outputs[2]
            polish = nt.nodes.new("ShaderNodeMix")
            polish.data_type = "FLOAT"
            polish.inputs[3].default_value = 0.2
            L.new(ramp.outputs["Color"], polish.inputs["Factor"])
            L.new(rough, polish.inputs[2])
            rough = polish.outputs[0]
        if ao is not None and color is not None and a.dirt > 0:
            dirt = nt.nodes.new("ShaderNodeMix")
            dirt.data_type = "RGBA"
            dirt.blend_type = "MULTIPLY"
            dirt.inputs["Factor"].default_value = min(1.0, 0.4 + 0.6 * a.dirt)
            L.new(color, dirt.inputs[6])
            L.new(ao.outputs["Color"], dirt.inputs[7])
            color = dirt.outputs[2]
        if color is not None:
            L.new(color, bsdf.inputs["Base Color"])
            L.new(rough, bsdf.inputs["Roughness"])
            L.new(metal, bsdf.inputs["Metallic"])

        # Relief : normal map calculée (DirectX -> OpenGL) + micro-relief des matériaux.
        if os.path.exists(base + "_normal.png"):
            nm = uv_tex(base + "_normal.png")
            sep = nt.nodes.new("ShaderNodeSeparateColor")
            inv = nt.nodes.new("ShaderNodeMath")
            inv.operation = "SUBTRACT"
            inv.inputs[0].default_value = 1.0
            comb = nt.nodes.new("ShaderNodeCombineColor")
            nmap = nt.nodes.new("ShaderNodeNormalMap")
            L.new(nm.outputs["Color"], sep.inputs["Color"])
            L.new(sep.outputs[0], comb.inputs[0])
            L.new(sep.outputs[1], inv.inputs[1])
            L.new(inv.outputs[0], comb.inputs[1])
            L.new(sep.outputs[2], comb.inputs[2])
            L.new(comb.outputs["Color"], nmap.inputs["Color"])
            normal = nmap.outputs["Normal"]
            if height is not None:
                bump = nt.nodes.new("ShaderNodeBump")
                bump.inputs["Strength"].default_value = 0.15
                bump.inputs["Distance"].default_value = 0.002
                L.new(height, bump.inputs["Height"])
                L.new(normal, bump.inputs["Normal"])
                normal = bump.outputs["Normal"]
            L.new(normal, bsdf.inputs["Normal"])

        if emis_mask is not None:
            mask, col = emis_mask
            bsdf.inputs["Emission Color"].default_value = (*[c ** 2.2 for c in col], 1.0)
            strength = nt.nodes.new("ShaderNodeMath")
            strength.operation = "MULTIPLY"
            strength.inputs[1].default_value = a.emission
            L.new(mask.outputs["Value"], strength.inputs[0])
            L.new(strength.outputs["Value"], bsdf.inputs["Emission Strength"])

    def set_zone(self, z, group):
        if z in self.groups:
            self.groups[z].node_tree = group


# -- scène -----------------------------------------------------------------------------------
def setup_scene(a, obj):
    scene = bpy.context.scene
    if a.cpu:
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = a.samples
    else:
        setup_cycles(scene, a.samples)
    scene.cycles.use_denoising = True
    scene.cycles.use_auto_tile = False
    scene.render.resolution_x = scene.render.resolution_y = a.size
    scene.world = bpy.data.worlds.new("studio")
    if bpy.app.version < (5, 0, 0):
        scene.world.use_nodes = True
    bg = next(n for n in scene.world.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs["Color"].default_value = (0.35, 0.37, 0.40, 1.0)
    bg.inputs["Strength"].default_value = 0.6
    for name, energy, rot in (("key", 4.0, (50, 0, 35)), ("rim", 3.0, (60, 0, 200)), ("fill", 1.0, (75, 0, -60))):
        light = bpy.data.objects.new(name, bpy.data.lights.new(name, "SUN"))
        light.data.energy = energy
        light.data.angle = math.radians(8)
        light.rotation_euler = tuple(math.radians(r) for r in rot)
        scene.collection.objects.link(light)
    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    pts = world_points(obj)
    lo, hi = pts.min(0), pts.max(0)
    center = (lo + hi) / 2
    diag = float(((hi - lo) ** 2).sum() ** 0.5)
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = float(max(hi - lo)) * 1.08
    cam.data.clip_end = 100 * diag
    cam.location = (center[0] + math.sin(math.radians(25)) * 3 * diag,
                    center[1] - math.cos(math.radians(25)) * 3 * diag, center[2])
    cam.rotation_euler = (math.radians(90), 0.0, math.radians(25))
    return scene


def zone_focus(obj, zid_path, z, diag):
    """Centre et taille d'un cadrage serré sur l'endroit où la zone `z` est la
    plus concentrée (pour juger les candidats des petites zones)."""
    import numpy as np

    im = bpy.data.images.load(zid_path, check_existing=True)
    w, h = im.size
    px = np.empty(w * h * 4, dtype=np.float32)
    im.pixels.foreach_get(px)
    ids = np.rint(px.reshape(h, w, 4)[..., 0] * 255).astype(int)  # ligne 0 = bas (v = 0)
    me = obj.data
    uv = me.uv_layers.active.data
    pts, nrms = [], []
    rot = obj.matrix_world.to_3x3()
    for poly in me.polygons:
        u = sum(uv[i].uv[0] for i in poly.loop_indices) / poly.loop_total
        v = sum(uv[i].uv[1] for i in poly.loop_indices) / poly.loop_total
        x, y = min(w - 1, max(0, int(u * w))), min(h - 1, max(0, int(v * h)))
        if ids[y, x] == z:
            pts.append(tuple(obj.matrix_world @ poly.center))
            nrms.append(tuple((rot @ poly.normal) * poly.area))
    if len(pts) < 3:
        return None
    pts, nrms = np.array(pts), np.array(nrms)
    r = 0.03 * diag
    density = ((pts[:, None] - pts[None]) ** 2).sum(-1) < r * r
    best = pts[density.sum(1).argmax()]
    sel = ((pts - best) ** 2).sum(-1) < (1.5 * r) ** 2
    near = pts[sel]
    size = float(max(np.ptp(near, 0).max() * 2.2, 0.07 * diag))
    n = nrms[sel].sum(0)
    n = n / (np.linalg.norm(n) or 1.0)
    return near.mean(0), size, n


def render_views(scene, cam, focus, path_full, path_out):
    """Vue d'ensemble, plus un gros plan si `focus`, côte à côte."""
    import numpy as np

    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
    from imgops import save_png

    shots = []
    keep = (tuple(cam.location), cam.data.ortho_scale)
    views = [None] + ([focus] if focus else [])
    for f in views:
        keep_rot = cam.rotation_euler.copy()
        if f is not None:
            from mathutils import Vector

            center, size, n = f
            d = Vector(tuple(n))  # caméra face aux surfaces de la zone
            cam.location = tuple(center[i] + d[i] * 3 * size for i in range(3))
            cam.rotation_euler = (-d).to_track_quat("-Z", "Z" if abs(d.z) < 0.9 else "Y").to_euler()
            cam.data.ortho_scale = size
        scene.render.filepath = path_full
        bpy.ops.render.render(write_still=True)
        im = bpy.data.images.load(path_full)
        px = np.empty(im.size[0] * im.size[1] * 4, dtype=np.float32)
        im.pixels.foreach_get(px)
        shots.append(px.reshape(im.size[1], im.size[0], 4)[..., :3])
        bpy.data.images.remove(im)
        cam.location, cam.data.ortho_scale = keep
        cam.rotation_euler = keep_rot
    save_png(path_out, np.concatenate(shots, 1))
    os.remove(path_full)


def candidate_group(z, c, zones):
    tile = c.get("tile_m")
    if tile is None and c.get("dimensions_mm"):
        tile = c["dimensions_mm"][0] / 1000.0
    return pbr_group(f"Zone{z}_{c['source']}_{c['id']}", c["maps"], tile or 0.5, zones[z][1] or (0.5, 0.5, 0.5))


def main():
    a = parse_args(sys.argv)
    base = os.path.join(a.maps_dir, a.name)
    zones = read_zones(base + "_zones.txt")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = import_objects(a.mesh)
    obj.name = a.name

    cands = json.load(open(base + "_candidates.json", encoding="utf-8")) if os.path.exists(base + "_candidates.json") else {}
    choice = read_choice(base + "_materials_choice.txt")
    final = json.load(open(base + "_materials_final.json", encoding="utf-8")) if os.path.exists(base + "_materials_final.json") else {}

    def chosen_group(z):
        mat, col = zones[z]
        pick, size = choice.get(z, ("procedural", None))
        if pick not in ("procedural", "emissif"):
            for c in [final.get(str(z))] + cands.get(str(z), {}).get("candidates", []):
                if c and f"{c['source']}:{c['id']}" == pick:
                    return candidate_group(z, dict(c, tile_m=size or c.get("tile_m")), zones)
            log(f"Zone {z} : « {pick} » introuvable dans les candidats, matériau calculé utilisé")
        return procedural_group(f"Zone{z}_procedural", mat, col)

    zm = ZoneMaterial(a, zones, chosen_group)
    obj.data.materials.clear()
    obj.data.materials.append(zm.mat)
    for p in obj.data.polygons:
        p.material_index = 0
    scene = setup_scene(a, obj)

    if a.candidates:
        out_dir = base + "_renders"
        os.makedirs(out_dir, exist_ok=True)
        pts = world_points(obj)
        diag = float(((pts.max(0) - pts.min(0)) ** 2).sum() ** 0.5)
        for zs, info in sorted(cands.items(), key=lambda t: int(t[0])):
            z = int(zs)
            keep = zm.groups[z].node_tree if z in zm.groups else None
            # Petite zone (rotules...) : gros plan en plus de la vue d'ensemble.
            focus = None
            if info.get("area_percent", 0) < 15:
                focus = zone_focus(obj, base + "_zones_id.png", z, diag)
            for k, c in enumerate(info["candidates"]):
                zm.set_zone(z, candidate_group(z, c, zones))
                render_views(scene, scene.camera, focus, os.path.join(out_dir, "_tmp.png"),
                             os.path.join(out_dir, f"zone{z}_{k}.png"))
                log(f"Zone {z}, candidat {k + 1} : {c['source']}:{c['id']}")
            if keep is not None:
                zm.set_zone(z, keep)
    if a.build:
        path = base + "_materials.blend"
        bpy.data.orphans_purge(do_recursive=True)  # matériau et image du .glb d'origine
        if a.pack:
            bpy.ops.file.pack_all()
        bpy.ops.wm.save_as_mainfile(filepath=path)
        log(f"Scène enregistrée : {path}")


if __name__ == "__main__":
    main()
