"""
Aperçu du mesh de jeu avec ses textures finales (étape 6) : rendu Cycles de
face, de 3/4 et de dos, éclairage studio.

    blender -b --factory-startup -P preview_material.py -- \
        --mesh robot_uv.glb --textures-dir dossier --name robot --output apercu.png
"""

import argparse
import math
import os
import sys

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
from bake_maps import import_objects, new_material, world_points  # noqa: E402
from imgops import save_png  # noqa: E402
from uv_optimize import setup_cycles  # noqa: E402


def parse_args(argv):
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    p = argparse.ArgumentParser(prog="preview_material")
    p.add_argument("--mesh", required=True)
    p.add_argument("--textures-dir", required=True)
    p.add_argument("--name", required=True, help="Nom des textures : T_<nom>_BC.png...")
    p.add_argument("--output", required=True)
    p.add_argument("--size", type=int, default=900)
    p.add_argument("--samples", type=int, default=64)
    p.add_argument("--cpu", action="store_true")
    return p.parse_args(argv)


def build_material(d, name):
    mat = new_material("preview_pbr")
    nt = mat.node_tree
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    def tex(suffix, color=False):
        path = os.path.join(d, f"T_{name}_{suffix}.png")
        if not os.path.exists(path):
            return None
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = bpy.data.images.load(path)
        node.image.colorspace_settings.name = "sRGB" if color else "Non-Color"
        return node

    bc = tex("BC", True)
    if bc:
        nt.links.new(bc.outputs["Color"], bsdf.inputs["Base Color"])
    orm = tex("ORM")
    if orm:
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(orm.outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs[1], bsdf.inputs["Roughness"])
        nt.links.new(sep.outputs[2], bsdf.inputs["Metallic"])
    nm = tex("N")
    if nm:  # DirectX -> OpenGL (Blender)
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        inv = nt.nodes.new("ShaderNodeMath")
        inv.operation = "SUBTRACT"
        inv.inputs[0].default_value = 1.0
        comb = nt.nodes.new("ShaderNodeCombineColor")
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(nm.outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs[0], comb.inputs[0])
        nt.links.new(sep.outputs[1], inv.inputs[1])
        nt.links.new(inv.outputs[0], comb.inputs[1])
        nt.links.new(sep.outputs[2], comb.inputs[2])
        nt.links.new(comb.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    em = tex("E", True)
    if em:
        nt.links.new(em.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 2.0
    return mat


def main():
    a = parse_args(sys.argv)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = import_objects(os.path.abspath(a.mesh))
    mat = build_material(os.path.abspath(a.textures_dir), a.name)
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for p in obj.data.polygons:
        p.material_index = 0
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
    scene.view_settings.view_transform = "AgX" if "AgX" in [i.identifier for i in scene.view_settings.bl_rna.properties["view_transform"].enum_items] else "Standard"
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
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    pts = world_points(obj)
    lo, hi = pts.min(0), pts.max(0)
    center = (lo + hi) / 2
    diag = float(np.linalg.norm(hi - lo))
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = float(max(hi - lo)) * 1.08
    cam.data.clip_end = 100 * diag
    tmp = os.path.abspath(a.output) + ".tmp.png"
    panels = []
    for angle in (0.0, 40.0, 180.0):
        r = math.radians(angle)
        cam.location = (center[0] + math.sin(r) * 3 * diag, center[1] - math.cos(r) * 3 * diag, center[2])
        cam.rotation_euler = (math.radians(90), 0.0, r)
        scene.render.filepath = tmp
        bpy.ops.render.render(write_still=True)
        im = bpy.data.images.load(tmp)
        px = np.empty(im.size[0] * im.size[1] * 4, dtype=np.float32)
        im.pixels.foreach_get(px)
        panels.append(px.reshape(im.size[1], im.size[0], 4)[..., :3])
        bpy.data.images.remove(im)
    os.remove(tmp)
    save_png(os.path.abspath(a.output), np.concatenate(panels, 1))
    print(f"[preview_material] Aperçu : {a.output}", flush=True)


if __name__ == "__main__":
    main()
