"""Tests de la reprojection de l'étape 4 (sans IA : les « vues générées »
sont les vues de position elles-mêmes, la texture doit donc retrouver la
position de chaque pixel)."""

import os
import sys

import numpy as np
import pytest

bpy = pytest.importorskip("bpy")
from PIL import Image  # noqa: E402

from test_uv_optimize import make_glb  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "texpipe", "ai"))
import color_guide as cg  # noqa: E402
from imgops import save_png  # noqa: E402


def test_views_project_back_onto_uv(tmp_path):
    mesh = tmp_path / "m_uv.glb"
    # Objet décentré et non symétrique : vérifie normalisation et caméras.
    make_glb(mesh, lambda: (bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, location=(0.3, 0, 1)),
                            bpy.ops.transform.resize(value=(1.0, 0.6, 1.4))))
    tp, tn, tuv = cg.load_glb(str(mesh))
    flat = tp.reshape(-1, 3)
    c = (flat.min(0) + flat.max(0)) / 2
    tpn = (tp - c) * (0.5 / np.abs(flat - c).max())
    views = cg.render_views(tpn, tn, 256)
    assert all(v["mask"].any() for v in views)
    save_png(str(tmp_path / "views.png"), np.concatenate([v["pos"] for v in views], 1)[::-1])
    rep = cg.run(cg.parse_args(["--mesh", str(mesh), "--views", str(tmp_path / "views.png"),
                                "--output-dir", str(tmp_path), "--texture-size", "512"]))
    assert rep["seen_percent"] > 95
    pos, _, cov = cg.texel_maps(tpn, tn, tuv, 512)
    col = np.asarray(Image.open(rep["color_guide"]), np.float32) / 255
    err = np.abs(col - np.clip(pos + 0.5, 0, 1)).max(-1)[cov]
    assert np.median(err) < 0.01
    assert np.percentile(err, 95) < 0.05


def test_background_removal():
    img = np.full((64, 64, 3), 250, np.uint8)
    img[40:60, :] = np.linspace(250, 200, 20)[:, None, None]  # ombre douce au sol
    img[10:30, 20:44] = (30, 20, 40)  # objet sombre
    out = np.asarray(cg.remove_background(Image.fromarray(img), 12))
    assert out[20, 30, 3] == 255 and out[2, 2, 3] == 0 and out[55, 5, 3] == 0
