"""Tests de non-régression du dépliage UV.

Ils utilisent Blender comme module Python :
    pip install bpy==5.2.2 numpy pytest   (Python 3.13)
    pytest tests/
"""

import json
import os
import subprocess
import sys

import pytest

bpy = pytest.importorskip("bpy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "texpipe", "blender", "uv_optimize.py")


def make_glb(path, builder):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    builder()
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True)


def run_optimizer(tmp_path, builder, *extra):
    src = tmp_path / "in.glb"
    out = tmp_path / "out.glb"
    make_glb(src, builder)
    cmd = [sys.executable, SCRIPT, "--", "--input", str(src), "--output", str(out),
           "--texture-size", "1024", "--no-transfer", *extra]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
    with open(tmp_path / "out_uv_report.json", encoding="utf-8") as fh:
        return json.load(fh), out


def count_islands_and_tris(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(path))
    obj = next(o for o in bpy.context.scene.objects if o.type == "MESH")
    me = obj.data
    return len(me.polygons), [l.name for l in me.uv_layers]


def check_invariants(rep):
    assert rep["hard_edges_not_seam"] == 0
    assert rep["flipped_triangles"] == 0
    assert rep["overlap_pixels_percent"] < 0.01
    assert rep["island_pairs_overlapping"] == []


def test_cylinder_is_three_islands(tmp_path):
    rep, _ = run_optimizer(tmp_path, lambda: bpy.ops.mesh.primitive_cylinder_add(vertices=16))
    check_invariants(rep)
    assert rep["islands"] == 3  # deux disques + un rectangle
    assert rep["distortion_mean"] < 0.02


def test_open_tube_unrolls_with_one_seam(tmp_path):
    rep, _ = run_optimizer(
        tmp_path, lambda: bpy.ops.mesh.primitive_cylinder_add(vertices=24, end_fill_type="NOTHING")
    )
    check_invariants(rep)
    assert rep["islands"] == 1
    assert rep["distortion_mean"] < 0.02
    # Un seul rectangle de rapport ~3:1 (2*pi*r / h) : il ne peut couvrir
    # qu'environ un tiers d'une texture carrée, et doit le faire entièrement.
    assert rep["uv_coverage_percent"] > 28


def test_sphere_and_torus_are_few_islands(tmp_path):
    rep, _ = run_optimizer(tmp_path, lambda: bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16))
    check_invariants(rep)
    assert rep["islands"] <= 4
    rep, _ = run_optimizer(tmp_path, lambda: bpy.ops.mesh.primitive_torus_add())
    check_invariants(rep)
    assert rep["islands"] <= 4


def test_triangulation_is_preserved(tmp_path):
    def build():
        bpy.ops.mesh.primitive_monkey_add()

    src = tmp_path / "in.glb"
    make_glb(src, build)
    n_in, _ = count_islands_and_tris(src)
    rep, out = run_optimizer(tmp_path, build)
    check_invariants(rep)
    n_out, uv_names = count_islands_and_tris(out)
    assert n_out == n_in  # même nombre de triangles qu'en entrée
    assert len(uv_names) == 1  # seulement les nouveaux UV


def test_hidden_density_halves_bottom(tmp_path):
    rep, _ = run_optimizer(tmp_path, lambda: bpy.ops.mesh.primitive_cube_add(), "--hidden-density", "0.5")
    td = rep["texel_density_px_per_m"]
    assert td["min"] == pytest.approx(td["max"] * 0.5, rel=0.15)
    rep, _ = run_optimizer(tmp_path, lambda: bpy.ops.mesh.primitive_cube_add(), "--hidden-density", "1.0")
    td = rep["texel_density_px_per_m"]
    assert td["min"] == pytest.approx(td["max"], rel=0.02)


def _mesh_stats(path):
    """Normales opposées au reflet attendu et part d'UV superposées
    entre les côtés gauche et droit."""
    import bmesh

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(path))
    obj = next(o for o in bpy.context.scene.objects if o.type == "MESH")
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    uv = bm.loops.layers.uv.active
    left, right = set(), set()
    for f in bm.faces:
        side = left if f.calc_center_median().x < 0 else right
        for l in f.loops:
            side.add(tuple(round(c, 5) for c in l[uv].uv))
    shared = len(left & right) / max(1, len(right))
    # Sur une sphère centrée, toute normale doit pointer vers l'extérieur.
    inverted = sum(1 for f in bm.faces if f.normal.dot(f.calc_center_median()) < 0)
    return shared, inverted


def test_symmetric_mesh_is_mirrored(tmp_path):
    rep, out = run_optimizer(tmp_path, lambda: bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16))
    check_invariants(rep)
    assert rep["symmetry"]["mode"] == "mirror"
    shared, inverted = _mesh_stats(out)
    assert shared > 0.99  # les deux moitiés partagent les mêmes UV
    assert inverted == 0  # la moitié recréée n'est pas retournée


def test_asymmetric_mesh_is_not_mirrored(tmp_path):
    def build():
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        bpy.ops.mesh.primitive_cube_add(size=0.5, location=(1.2, 0, 0.5))

    rep, _ = run_optimizer(tmp_path, build)
    check_invariants(rep)
    assert rep["symmetry"]["mode"] == "off"
    rep, out = run_optimizer(tmp_path, lambda: bpy.ops.mesh.primitive_uv_sphere_add(), "--symmetry", "off")
    assert rep["symmetry"]["mode"] == "off"
    shared, _ = _mesh_stats(out)
    assert shared < 0.5
