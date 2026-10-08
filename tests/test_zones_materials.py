"""Tests des étapes 5 (zones) et 6 (matériaux)."""

import os
import sys
import types

import numpy as np
import pytest

bpy = pytest.importorskip("bpy")
from PIL import Image  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "texpipe", "ai"))
import materials as mt  # noqa: E402
import zones as zn  # noqa: E402

from test_bake_maps import bake  # noqa: E402


def test_zone_file_roundtrip(tmp_path):
    zs = [dict(id=1, material="metal_sombre", color="#282828", area_percent=90.0),
          dict(id=2, material="emissif", color="#a836f9", area_percent=10.0)]
    path = tmp_path / "z.txt"
    zn.write_zone_file(str(path), "x", zs)
    out = zn.read_zone_file(str(path))
    assert out[1][0] == "metal_sombre" and out[2][0] == "emissif"
    assert out[2][1] == pytest.approx((0xA8 / 255, 0x36 / 255, 0xF9 / 255))
    path.write_text("1 = inconnu\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        zn.read_zone_file(str(path))


def test_flat_colors_ignore_antialiased_edges():
    black, purple, gray = np.array([0.16, 0.16, 0.16]), np.array([0.66, 0.21, 0.98]), np.array([0.41, 0.41, 0.40])
    px = np.concatenate([np.repeat(black[None], 8000, 0), np.repeat(purple[None], 1500, 0),
                         np.repeat(gray[None], 400, 0),
                         np.linspace(black, purple, 200),  # bords anticrénelés
                         np.repeat(np.array([[0.85, 0.85, 0.85]]), 10, 0)])  # mêlés au fond
    a = types.SimpleNamespace(seed=0, zones=0, merge=12)
    labels, colors = zn.classify_flat_colors([px], a)
    assert len(colors) == 3


def test_zones_and_materials_pipeline(tmp_path):
    rep = bake(tmp_path)  # cartes de l'étape 3 sur une sphère
    zones = zn.run(zn.parse_args(["--mesh", str(tmp_path / "low_uv.glb"), "--size", "256"]))
    assert len(zones) >= 1 and os.path.exists(tmp_path / "low_zones_preview.png")
    res = mt.run(mt.parse_args(["--mesh", str(tmp_path / "low_uv.glb"), "--noise-res", "256"]))
    for key in ("BC", "N", "ORM"):
        assert os.path.exists(res["textures"][key])
    orm = np.asarray(Image.open(res["textures"]["ORM"]), np.float32) / 255
    assert 0.0 <= orm[..., 1].min() and orm[..., 1].max() <= 1.0
    assert rep["texture_size"] == 256
