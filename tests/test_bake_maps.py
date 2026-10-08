"""Tests du calcul des cartes (étape 3).

    pip install bpy==5.2.2 numpy pytest   (Python 3.13)
    pytest tests/test_bake_maps.py
"""

import json
import os
import subprocess
import sys

import pytest

bpy = pytest.importorskip("bpy")
import numpy as np  # noqa: E402

from test_uv_optimize import make_glb  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UV_SCRIPT = os.path.join(ROOT, "texpipe", "blender", "uv_optimize.py")
BAKE_SCRIPT = os.path.join(ROOT, "texpipe", "blender", "bake_maps.py")


def low_sphere():
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12)


def high_sphere():
    """Sphère dense avec un relief en bandes (symétrique gauche/droite)."""
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6)
    obj = bpy.context.active_object
    for v in obj.data.vertices:
        z = v.co.z
        v.co *= 1.0 + 0.02 * np.sin(40 * z)


def run(cmd):
    res = subprocess.run([sys.executable, *cmd], capture_output=True, text=True, timeout=900)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
    return res


def bake(tmp_path, *uv_extra):
    low, high = tmp_path / "low.glb", tmp_path / "high.glb"
    make_glb(low, low_sphere)
    make_glb(high, high_sphere)
    low_uv = tmp_path / "low_uv.glb"
    run([UV_SCRIPT, "--", "--input", str(low), "--output", str(low_uv), "--texture-size", "256",
         "--no-transfer", *uv_extra])
    run([BAKE_SCRIPT, "--", "--low", str(low_uv), "--high", str(high), "--texture-size", "256",
         "--samples", "1", "--ao-samples", "4", "--cpu"])
    with open(tmp_path / "low_bake_report.json", encoding="utf-8") as fh:
        return json.load(fh)


def read_png(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    im = bpy.data.images.load(str(path))
    px = np.empty(im.size[0] * im.size[1] * 4, dtype=np.float32)
    im.pixels.foreach_get(px)
    return px.reshape(im.size[1], im.size[0], 4)


def test_symmetric_mesh_bakes_one_half(tmp_path):
    rep = bake(tmp_path)
    # La moitié recréée en miroir réutilise les pixels de l'autre.
    assert rep["faces_sharing_uv"] > 0
    assert rep["uv_overlap_percent"] < 0.1
    assert rep["pixels_missed_percent"] < 1.0
    for kind in ("normal", "ao", "curvature", "height", "up", "basecolor_high"):
        assert os.path.exists(rep["maps"][kind]), kind
    with open(rep["maps"]["normal"], "rb") as fh:
        assert fh.read(26)[24] == 16  # normal map en 16 bits
    nrm = read_png(rep["maps"]["normal"])
    # Le relief en bandes doit apparaître (normale non uniforme), et la
    # normale moyenne rester proche de (0, 0, 1).
    assert nrm[..., 1].std() > 0.02
    assert abs(nrm[..., 2].mean() - 1.0) < 0.15


def test_asymmetric_uv_bakes_everything(tmp_path):
    rep = bake(tmp_path, "--symmetry", "off")
    assert rep["faces_sharing_uv"] == 0
    assert rep["uv_overlap_percent"] < 0.1
    assert rep["pixels_missed_percent"] < 1.0
