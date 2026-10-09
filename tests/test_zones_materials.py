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


def _fake_library(folder):
    """Deux matériaux factices, au format AmbientCG et Poly Haven."""
    rng = np.random.default_rng(0)
    for name, prefix, keys, col in (("ambientcg_Metal001", "Metal001_1K-JPG_",
                                     ("Color", "Roughness", "Metalness", "NormalGL"), (40, 40, 42)),
                                    ("polyhaven_metal_plate", "ph_", ("diffuse", "rough", "metal", "nor_gl"), (110, 110, 115))):
        d = folder / name
        d.mkdir(parents=True)
        base = np.clip(np.array(col)[None, None] + rng.normal(0, 8, (64, 64, 1)), 0, 255).astype(np.uint8)
        Image.fromarray(np.repeat(base, 3, 2) if base.shape[2] == 1 else base).save(d / f"{prefix}{keys[0]}.jpg")
        for k, v in zip(keys[1:], (100, 255, 128)):
            Image.fromarray(np.full((64, 64), v, np.uint8)).save(d / f"{prefix}{k}.jpg")


def test_photo_materials_offline(tmp_path, monkeypatch):
    import find_materials as fm

    bake(tmp_path)
    zn.run(zn.parse_args(["--mesh", str(tmp_path / "low_uv.glb"), "--size", "256"]))
    lib = tmp_path / "lib"
    _fake_library(lib)
    monkeypatch.setattr(fm, "ambientcg_search", lambda q, n: [dict(source="ambientcg", id="Metal001", name="Metal001", tags=[])])
    monkeypatch.setattr(fm, "polyhaven_search", lambda w, n: [dict(source="polyhaven", id="metal_plate", name="Metal Plate", tags=[])])
    args = ["--mesh", str(tmp_path / "low_uv.glb"), "--library", str(lib)]
    res = fm.run(fm.parse_args(args))
    assert all(len(v["candidates"]) == 2 for v in res.values())
    assert fm.map_kind("Metal001_1K-JPG_NormalDX.jpg") is None and fm.map_kind("ph_nor_gl.jpg") == "normal"
    choice = fm.read_choice(str(tmp_path / "low_materials_choice.txt"))
    assert all(":" in v or v in ("emissif", "procedural") for v in choice.values())
    fm.finalize(fm.parse_args(args))  # hors ligne : la version 1K est gardée
    script = os.path.join(ROOT, "texpipe", "blender", "material_scene.py")
    import subprocess

    r = subprocess.run([sys.executable, script, "--", "--mesh", str(tmp_path / "low_uv.glb"), "--build", "--cpu"],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert (tmp_path / "low_materials.blend").exists()
