"""Opérations d'image en numpy pur, partagées par les scripts Blender et
les scripts IA du pipeline (aucune dépendance à Blender ni à torch)."""

import os
import zlib

import numpy as np


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


def pipeline_version():
    """Version du pipeline (texpipe/VERSION.txt), affichée au lancement."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return "inconnue"
