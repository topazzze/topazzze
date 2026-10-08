"""
uv_optimize.py - Dépliage UV de qualité production pour meshes low poly de jeu.

Pensé pour les meshes « Smart Low Poly » de Tripo3D (surfaces dures : robots,
décors industriels, pierre). Se lance dans Blender en mode console :

    blender -b --factory-startup -P texpipe/blender/uv_optimize.py -- \
        --input robot_low.glb --output robot_low_uv.glb --texture-size 4096

Ce que fait le script, dans l'ordre :

 1. Nettoyage : fusion des objets, soudure des sommets dupliqués, normales
    recalculées, arêtes vives (hard edges) définies par angle.
 2. Coût des coutures : chaque arête reçoit un coût selon sa visibilité
    (occlusion ambiante + orientation : dos et dessous sont « cachés »)
    et sa forme (les creux et les plis cachent bien une couture).
 3. Coutures obligatoires : toute arête vive est une couture (règle
    indispensable pour un bake de normal map propre).
 4. Topologie : chaque îlot doit être un disque. Les tubes (bras, pistons)
    reçoivent UNE couture le long de leur côté le moins visible ; les formes
    fermées (rotules) sont coupées en deux selon leurs normales.
 5. Distorsion : dépliage « Minimum Stretch » (SLIM), mesure de l'étirement
    par triangle ; les îlots trop déformés sont redécoupés, jusqu'à
    convergence.
 6. Fusion : les petits îlots sont recollés à leurs voisins quand la
    distorsion reste acceptable (moins de coutures, moins de fragments).
 7. Redressement : les îlots en grille de quads sont redressés en rectangles
    parfaits (meilleur rendu des textures répétées, packing plus dense).
 8. Densité de texels uniforme, avec option pour réduire la résolution des
    zones cachées (dessous, intérieur), puis orientation et packing serré
    avec une marge en pixels adaptée à la taille de texture (mipmaps).
 9. Contrôle : rapport JSON (distorsion, densité de texels, remplissage,
    chevauchements), image de la disposition UV, rendu damier optionnel,
    et transfert de l'ancienne texture Tripo sur les nouveaux UV.
"""

import argparse
import heapq
import json
import math
import os
import sys
import time
from collections import deque

import bpy  # doit précéder bmesh quand bpy est utilisé comme module Python
import bmesh
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

# Seuils de distorsion (moyenne, 95e centile) par îlot. Repères mesurés :
# demi-sphère 0.16 / 0.34, quart de sphère 0.06 / 0.15, tore 0.10 / 0.16.
PRESETS = {
    "seams": (0.14, 0.40),
    "balanced": (0.10, 0.30),
    "distortion": (0.06, 0.18),
}

ORIGINAL_UV_NAME = "UV_Original"
NEW_UV_NAME = "UVMap"


def log(msg):
    print(f"[uv_optimize] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def parse_args(argv):
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    p = argparse.ArgumentParser(
        prog="uv_optimize",
        description="Dépliage UV de qualité production pour meshes low poly.",
    )
    p.add_argument("--input", required=True, help="Mesh d'entrée (.glb, .gltf, .fbx, .obj, .blend)")
    p.add_argument("--output", required=True, help="Mesh de sortie (.glb, .fbx, .obj ou .blend)")
    p.add_argument("--texture-size", type=int, default=4096, help="Résolution visée (défaut 4096)")
    p.add_argument("--padding", type=int, default=None, help="Marge entre îlots en pixels (défaut : taille/256)")
    p.add_argument("--sharp-angle", default="auto", help="Angle (°) au-delà duquel une arête est vive et devient une couture. "
                   "« auto » (défaut) : 65°, relevé automatiquement sur les meshes très facettés (Tripo)")
    p.add_argument("--symmetry", default="auto", choices=["auto", "mirror", "off"],
                   help="Symétrie gauche/droite : auto (défaut) = détectée puis appliquée si le mesh est symétrique ; "
                        "mirror = forcée ; off = chaque côté a ses propres UV (détails asymétriques possibles)")
    p.add_argument("--quality", default="balanced", choices=sorted(PRESETS), help="Compromis coutures / distorsion : seams (moins de coutures), balanced (défaut), distortion (étirement minimal)")
    p.add_argument("--max-distortion", type=float, default=None, help="Distorsion moyenne max par îlot (remplace le préréglage ; 0.10 ≈ 10 %%)")
    p.add_argument("--max-distortion-p95", type=float, default=None, help="Distorsion max au 95e centile par îlot (remplace le préréglage)")
    p.add_argument("--max-iterations", type=int, default=30, help="Itérations max de redécoupage")
    p.add_argument("--min-compactness", type=float, default=0.45, help="Compacité min d'un îlot (aire / enveloppe convexe), pour un rangement serré (défaut 0.45)")
    p.add_argument("--hidden-density", type=float, default=0.5, help="Densité de texels des zones cachées, 1.0 = uniforme (défaut 0.5)")
    p.add_argument("--front", default="-Y", choices=["-Y", "+Y", "-X", "+X"], help="Face avant du mesh dans Blender (défaut -Y, convention glTF)")
    p.add_argument("--ao-rays", type=int, default=32, help="Rayons par sommet pour l'estimation de visibilité")
    p.add_argument("--quadify", dest="quadify", action="store_true", default=None, help="Forcer les quads temporaires (auto par défaut : activé si le mesh est en triangles)")
    p.add_argument("--no-quadify", dest="quadify", action="store_false", help="Ne pas reconstruire de quads temporaires")
    p.add_argument("--keep-quads", action="store_true", help="Garder les quads au lieu de restaurer la triangulation d'origine")
    p.add_argument("--min-hard-chain", type=float, default=0.03, help="Longueur min d'une ligne d'arêtes vives, en fraction de la diagonale (défaut 0.03)")
    p.add_argument("--min-island", type=float, default=0.0015, help="Surface min d'un îlot délimité par des arêtes vives, en fraction de la surface totale (défaut 0.0015)")
    p.add_argument("--no-merge", action="store_true", help="Désactiver la fusion des petits îlots")
    p.add_argument("--no-straighten", action="store_true", help="Désactiver le redressement des grilles de quads")
    p.add_argument("--no-transfer", action="store_true", help="Ne pas transférer l'ancienne texture sur les nouveaux UV")
    p.add_argument("--keep-original-uv", action="store_true", help="Garder les UV d'origine en 2e canal dans le fichier exporté")
    p.add_argument("--save-blend", default=None, help="Sauver aussi la scène .blend (contient les anciens UV)")
    p.add_argument("--report", default=None, help="Rapport JSON (défaut : <output>_uv_report.json)")
    p.add_argument("--layout", default=None, help="Image de la disposition UV (défaut : <output>_uv_layout.png)")
    p.add_argument("--preview", default=None, help="Rendu damier de contrôle (.png), optionnel, plus lent")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)
    preset = PRESETS[args.quality]
    if args.max_distortion is None:
        args.max_distortion = preset[0]
    if args.max_distortion_p95 is None:
        args.max_distortion_p95 = preset[1]
    if args.padding is None:
        args.padding = max(4, args.texture_size // 256)
    # Chemins absolus dès le départ : Blender résout les chemins relatifs
    # depuis la racine du disque (ex. C:\), pas depuis le dossier courant.
    for name in ("input", "output", "report", "layout", "preview", "save_blend"):
        value = getattr(args, name)
        if value:
            setattr(args, name, os.path.abspath(value))
    base = os.path.splitext(args.output)[0]
    if args.report is None:
        args.report = base + "_uv_report.json"
    if args.layout is None:
        args.layout = base + "_uv_layout.png"
    return args


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------


def import_mesh(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        if ext in (".glb", ".gltf"):
            bpy.ops.import_scene.gltf(filepath=path)
        elif ext == ".fbx":
            bpy.ops.import_scene.fbx(filepath=path)
        elif ext == ".obj":
            bpy.ops.wm.obj_import(filepath=path)
        else:
            raise SystemExit(f"Format non supporté : {ext}")
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        raise SystemExit("Aucun mesh trouvé dans le fichier d'entrée.")
    return join_meshes(meshes)


def join_meshes(meshes):
    """Fusionne tous les meshes en un seul objet, transformations appliquées."""
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    # Détache l'objet de toute hiérarchie (le glTF ajoute souvent des parents)
    # en gardant sa position dans le monde, puis applique la transformation.
    world = obj.matrix_world.copy()
    obj.parent = None
    obj.matrix_world = world
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return obj


def export_mesh(obj, args):
    path = args.output
    ext = os.path.splitext(path)[1].lower()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    if ext in (".glb", ".gltf"):
        bpy.ops.export_scene.gltf(
            filepath=path,
            export_format="GLB" if ext == ".glb" else "GLTF_SEPARATE",
            use_selection=True,
            export_texcoords=True,
            export_normals=True,
            export_materials="EXPORT",
        )
    elif ext == ".fbx":
        bpy.ops.export_scene.fbx(
            filepath=path,
            use_selection=True,
            mesh_smooth_type="FACE",
            path_mode="COPY",
            embed_textures=False,
        )
    elif ext == ".obj":
        bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True)
    elif ext == ".blend":
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(path))
    else:
        raise SystemExit(f"Format de sortie non supporté : {ext}")


# ---------------------------------------------------------------------------
# Nettoyage
# ---------------------------------------------------------------------------


def preprocess(obj, args):
    """Soudure, normales, arêtes vives, couches UV. Retourne l'objet prêt."""
    me = obj.data
    # Les normales personnalisées importées (glTF) masquent les arêtes vives :
    # on les remplace par des arêtes vives définies par angle.
    if me.has_custom_normals:
        bpy.ops.mesh.customdata_custom_splitnormals_clear()

    # Couches UV : l'ancienne est conservée sous un autre nom, pour le
    # transfert de texture ; la nouvelle devient la couche active.
    if me.uv_layers:
        me.uv_layers[0].name = ORIGINAL_UV_NAME
        while len(me.uv_layers) > 1:
            me.uv_layers.remove(me.uv_layers[1])
    new_uv = me.uv_layers.new(name=NEW_UV_NAME)
    me.uv_layers.active = new_uv
    new_uv.active_render = True

    bm = bmesh.new()
    bm.from_mesh(me)
    diag = bbox_diagonal(bm)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=max(diag * 1e-6, 1e-7))
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bmesh.ops.dissolve_degenerate(bm, dist=max(diag * 1e-7, 1e-8), edges=bm.edges)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    # Symétrie : on ne garde qu'une moitié, dépliée seule (toute la texture
    # pour elle), puis recopiée en miroir à la fin avec les mêmes UV.
    args.sym = detect_symmetry(bm, args)
    if args.sym:
        bmesh.ops.translate(bm, verts=bm.verts, vec=(-args.sym["plane_x"], 0.0, 0.0))

    # Quads temporaires : le glTF ne stocke que des triangles, or le
    # redressement des îlots en grille a besoin de quads. Ils sont
    # reconstruits sur le mesh complet (avant la coupe de symétrie, qui
    # mélangerait l'ordre des faces). Les arêtes d'origine sont mémorisées
    # par position pour restaurer la même triangulation à la fin (le bake de
    # normal map doit voir la triangulation du moteur).
    original_edges = None
    n_tri = sum(1 for f in bm.faces if len(f.verts) == 3)
    quadify = args.quadify if args.quadify is not None else n_tri > 0.6 * max(len(bm.faces), 1)
    paired = 0
    if quadify:
        original_edges = {edge_key(e) for e in bm.edges}
        paired = rebuild_quads_from_order(bm)

    if args.sym:
        # Visibilité calculée sur le mesh complet, pas sur la moitié.
        args.full_tree = BVHTree.FromBMesh(bm)
        bmesh.ops.bisect_plane(
            bm,
            geom=bm.verts[:] + bm.edges[:] + bm.faces[:],
            dist=diag * 1e-6,
            plane_co=(0.0, 0.0, 0.0),
            plane_no=(1.0, 0.0, 0.0),
            clear_inner=True,
        )
        for v in bm.verts:
            if abs(v.co.x) < diag * 1e-5:
                v.co.x = 0.0
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
        log(f"Symétrie détectée (plan x = {args.sym['plane_x']:.4f}, écart moyen {100 * args.sym['error_mean']:.3f} % de la taille) : "
            f"une seule moitié est dépliée, l'autre sera superposée en miroir")

    if quadify:
        bmesh.ops.join_triangles(
            bm,
            faces=[f for f in bm.faces if len(f.verts) == 3],
            angle_face_threshold=math.radians(25),
            angle_shape_threshold=math.radians(35),
        )
        n_quads = sum(1 for f in bm.faces if len(f.verts) == 4)
        log(f"Quads temporaires : {n_quads} quads ({paired} retrouvés d'après l'ordre du fichier), "
            f"{len(bm.faces) - n_quads} autres faces")
    bm.verts.index_update()

    args.sharp_angle = resolve_sharp_angle(bm, args.sharp_angle)
    sharp_limit = math.radians(args.sharp_angle)
    for f in bm.faces:
        f.smooth = True
    for e in bm.edges:
        e.seam = False
        if len(e.link_faces) != 2:
            e.smooth = False
            continue
        e.smooth = e.calc_face_angle(0.0) < sharp_limit
    softened = soften_short_sharp_chains(bm, diag * args.min_hard_chain)
    if softened:
        log(f"Arêtes vives isolées adoucies : {softened}")
    bm.to_mesh(me)
    bm.free()
    me.update()
    return original_edges


def detect_symmetry(bm, args):
    """Cherche un plan de symétrie gauche/droite (perpendiculaire à X). Les
    meshes Tripo générés de face sont presque toujours symétriques."""
    if args.symmetry == "off" or not bm.verts:
        return None
    co = np.array([v.co[:] for v in bm.verts])
    diag = float(np.linalg.norm(co.max(0) - co.min(0))) or 1.0
    tree = BVHTree.FromBMesh(bm)
    rng = np.random.default_rng(args.seed)
    idx = rng.choice(len(co), min(3000, len(co)), replace=False)
    best = None
    for c in (0.0, float((co[:, 0].min() + co[:, 0].max()) / 2), float(np.median(co[:, 0]))):
        pts = co[idx].copy()
        pts[:, 0] = 2 * c - pts[:, 0]
        d = np.array([(tree.find_nearest(Vector(p))[0] - Vector(p)).length for p in pts]) / diag
        if best is None or d.mean() < best[1].mean():
            best = (c, d)
    c, d = best
    sym = {"plane_x": c, "error_mean": float(d.mean()), "error_p95": float(np.percentile(d, 95))}
    ok = sym["error_mean"] < 0.001 and sym["error_p95"] < 0.005
    if not ok:
        if args.symmetry == "mirror":
            log(f"ATTENTION : symétrie forcée sur un mesh peu symétrique (écart moyen {100 * sym['error_mean']:.2f} %)")
            return sym
        log(f"Pas de symétrie gauche/droite (écart moyen {100 * sym['error_mean']:.2f} %) : dépliage complet")
        return None
    return sym


def mirror_half(obj, args):
    """Recrée la moitié manquante en miroir. Les UV sont recopiées à
    l'identique : les deux côtés partagent les mêmes pixels (densité de
    texels doublée), et la jonction au plan de symétrie est invisible."""
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    diag = bbox_diagonal(bm)
    tol = diag * 1e-5
    # Copie exacte de la moitié, reflétée en X. Un reflet inverse
    # l'orientation de chaque face : toutes les copies sont donc retournées,
    # ce qui est juste par construction (pas d'heuristique). Les UV suivent
    # les coins et restent identiques à l'original.
    dup = bmesh.ops.duplicate(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:])
    new_verts = [g for g in dup["geom"] if isinstance(g, bmesh.types.BMVert)]
    new_faces = [g for g in dup["geom"] if isinstance(g, bmesh.types.BMFace)]
    for v in new_verts:
        v.co.x = -v.co.x
    bmesh.ops.reverse_faces(bm, faces=new_faces)
    center = [v for v in bm.verts if abs(v.co.x) < tol]
    bmesh.ops.remove_doubles(bm, verts=center, dist=tol)
    # Les arêtes du plan de symétrie redeviennent intérieures : lissées
    # selon l'angle (sinon une arête vive parasite au milieu du mesh).
    limit = math.radians(args.sharp_angle)
    for e in bm.edges:
        if len(e.link_faces) == 2 and all(abs(v.co.x) < tol * 10 for v in e.verts):
            e.smooth = e.calc_face_angle(0.0) < limit
    bmesh.ops.translate(bm, verts=bm.verts, vec=(args.sym["plane_x"], 0.0, 0.0))
    bm.to_mesh(me)
    bm.free()
    me.update()


def resolve_sharp_angle(bm, value):
    """Seuil d'arête vive. En « auto » : 65° sur un mesh classique. Sur un
    low poly très facetté (générateurs IA : un quart des arêtes au-delà de
    65°), ces angles sont des facettes d'approximation, pas des arêtes
    dessinées ; le seuil monte alors pour ne garder que les ~5 % d'arêtes les
    plus vives (bords de plaques, lames), entre 65° et 120°."""
    if str(value).lower() != "auto":
        return float(value)
    ang = np.array([math.degrees(e.calc_face_angle(0.0)) for e in bm.edges if len(e.link_faces) == 2])
    if len(ang) == 0 or (ang > 65.0).mean() <= 0.10:
        return 65.0
    angle = float(np.clip(np.percentile(ang, 95), 65.0, 120.0))
    log(f"Mesh très facetté ({100 * (ang > 65.0).mean():.0f} % d'arêtes > 65°) : seuil d'arête vive relevé à {angle:.0f}°")
    return angle


def rebuild_quads_from_order(bm):
    """Un mesh en quads exporté en glTF est écrit en triangles, chaque quad
    donnant deux triangles consécutifs. On les réunit dans cet ordre (avec
    recalage quand un vrai triangle décale les paires) : on retrouve ainsi
    exactement les quads d'origine, sans les deviner."""
    bm.faces.ensure_lookup_table()
    faces = list(bm.faces)
    pairs = []
    k = 0
    while k < len(faces) - 1:
        a, b = faces[k], faces[k + 1]
        if len(a.verts) == 3 and len(b.verts) == 3:
            shared = [e for e in a.edges if b in e.link_faces]
            if len(shared) == 1 and len(shared[0].link_faces) == 2 and quad_is_convex(a, b, shared[0]):
                pairs.append((a, b))
                k += 2
                continue
        k += 1
    n = 0
    for a, b in pairs:
        if a.is_valid and b.is_valid and bmesh.utils.face_join([a, b]) is not None:
            n += 1
    return n


def quad_is_convex(a, b, edge):
    """Le quad formé par deux triangles est-il convexe et non replié ?"""
    va = [v for v in a.verts if v not in edge.verts][0]
    vb = [v for v in b.verts if v not in edge.verts][0]
    if a.normal.dot(b.normal) < 0.0:
        return False
    d = vb.co - va.co
    p0, p1 = edge.verts[0].co, edge.verts[1].co
    n = (a.normal + b.normal).normalized()
    # Les deux sommets opposés doivent être de part et d'autre de la diagonale.
    s0 = (p0 - va.co).cross(d).dot(n)
    s1 = (p1 - va.co).cross(d).dot(n)
    return s0 * s1 < 0


def soften_short_sharp_chains(bm, min_length):
    """Les arêtes vives qui ne forment pas une ligne continue assez longue
    (bruit de génération IA) sont adoucies : elles créeraient sinon des
    micro-îlots. Le relief correspondant passera par la normal map."""
    sharp = [e for e in bm.edges if not e.smooth and len(e.link_faces) == 2]
    parent = {e.index: e.index for e in sharp}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_vert = {}
    for e in sharp:
        for v in e.verts:
            by_vert.setdefault(v.index, []).append(e.index)
    for edges in by_vert.values():
        for i in edges[1:]:
            a, b = find(edges[0]), find(i)
            if a != b:
                parent[a] = b
    length = {}
    for e in sharp:
        r = find(e.index)
        length[r] = length.get(r, 0.0) + e.calc_length()
    n = 0
    for e in sharp:
        if length[find(e.index)] < min_length:
            e.smooth = True
            n += 1
    return n


def pair_key(a, b):
    """Identifiant d'une arête par la position de ses sommets (stable même
    quand la coupe de symétrie renumérote les sommets)."""
    return frozenset((tuple(round(c, 6) for c in a.co), tuple(round(c, 6) for c in b.co)))


def edge_key(e):
    return pair_key(e.verts[0], e.verts[1])


def restore_triangulation(obj, original_edges):
    """Recoupe les quads temporaires selon leur diagonale d'origine. Les UV
    des coins sont conservés tels quels."""
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.ensure_lookup_table()
    n = 0
    for f in list(bm.faces):
        if len(f.verts) != 4:
            continue
        v = list(f.verts)
        if pair_key(v[0], v[2]) in original_edges:
            pair = (v[0], v[2])
        elif pair_key(v[1], v[3]) in original_edges:
            pair = (v[1], v[3])
        else:
            continue
        res = bmesh.ops.connect_verts(bm, verts=list(pair))
        for e in res["edges"]:
            e.smooth = True
            e.seam = False
        n += 1
    bm.to_mesh(me)
    bm.free()
    me.update()
    return n


def bbox_diagonal(bm):
    if not bm.verts:
        return 1.0
    co = np.array([v.co[:] for v in bm.verts])
    return float(np.linalg.norm(co.max(0) - co.min(0))) or 1.0


# ---------------------------------------------------------------------------
# Structure topologique figée (la topologie ne change plus après nettoyage)
# ---------------------------------------------------------------------------


class Topology:
    def __init__(self, bm, args):
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.index_update()
        self.V, self.E, self.F = len(bm.verts), len(bm.edges), len(bm.faces)

        # Coins (loops) numérotés globalement, face par face.
        k = 0
        self.face_loop_start = np.zeros(self.F + 1, dtype=np.int64)
        for f in bm.faces:
            self.face_loop_start[f.index] = k
            for l in f.loops:
                l.index = k
                k += 1
        self.face_loop_start[self.F] = k
        self.L = k
        self.loop_face = np.zeros(k, dtype=np.int64)
        self.loop_vert = np.zeros(k, dtype=np.int64)
        self.loop_edge = np.zeros(k, dtype=np.int64)
        self.loop_next = np.zeros(k, dtype=np.int64)
        self.loop_radial = np.full(k, -1, dtype=np.int64)
        for f in bm.faces:
            for l in f.loops:
                i = l.index
                self.loop_face[i] = f.index
                self.loop_vert[i] = l.vert.index
                self.loop_edge[i] = l.edge.index
                self.loop_next[i] = l.link_loop_next.index
                if len(l.edge.link_faces) == 2:
                    r = l.link_loop_radial_next
                    self.loop_radial[i] = r.index

        self.vert_co = np.array([v.co[:] for v in bm.verts], dtype=np.float64)
        self.vert_no = np.array([v.normal[:] for v in bm.verts], dtype=np.float64)
        self.face_no = np.array([f.normal[:] for f in bm.faces], dtype=np.float64)
        self.face_area = np.array([f.calc_area() for f in bm.faces], dtype=np.float64)
        self.face_center = np.array([f.calc_center_median()[:] for f in bm.faces], dtype=np.float64)
        self.face_is_quad = np.array([len(f.verts) == 4 for f in bm.faces])
        self.face_material = np.array([f.material_index for f in bm.faces], dtype=np.int64)
        self.face_edges = [[e.index for e in f.edges] for f in bm.faces]
        self.face_verts = [[v.index for v in f.verts] for f in bm.faces]

        self.edge_verts = np.array([[e.verts[0].index, e.verts[1].index] for e in bm.edges], dtype=np.int64)
        self.edge_faces = np.full((self.E, 2), -1, dtype=np.int64)
        self.edge_len = np.array([e.calc_length() for e in bm.edges], dtype=np.float64)
        self.edge_angle = np.zeros(self.E, dtype=np.float64)  # signé : >0 convexe
        self.vert_edges = [[] for _ in range(self.V)]
        mandatory = np.zeros(self.E, dtype=bool)
        sharp = np.zeros(self.E, dtype=bool)
        for e in bm.edges:
            i = e.index
            lf = e.link_faces
            for j, f in enumerate(lf[:2]):
                self.edge_faces[i, j] = f.index
            self.vert_edges[e.verts[0].index].append(i)
            self.vert_edges[e.verts[1].index].append(i)
            if len(lf) != 2:
                mandatory[i] = True
                continue
            self.edge_angle[i] = e.calc_face_angle_signed(0.0)
            if not e.smooth:
                sharp[i] = True
                mandatory[i] = True
            if lf[0].material_index != lf[1].material_index:
                mandatory[i] = True
            # Enroulement incohérent : la marche le long des bords en dépend.
            l0 = e.link_loops[0]
            if l0.link_loop_radial_next.vert == l0.vert:
                mandatory[i] = True
        self.mandatory = mandatory
        self.sharp = sharp

        # Triangulation (pour les mesures de distorsion), par coins globaux.
        tris = bm.calc_loop_triangles()
        self.tri_loops = np.array([[t[0].index, t[1].index, t[2].index] for t in tris], dtype=np.int64)
        self.tri_face = self.loop_face[self.tri_loops[:, 0]]
        p0 = self.vert_co[self.loop_vert[self.tri_loops[:, 0]]]
        p1 = self.vert_co[self.loop_vert[self.tri_loops[:, 1]]]
        p2 = self.vert_co[self.loop_vert[self.tri_loops[:, 2]]]
        e1, e2 = p1 - p0, p2 - p0
        l1 = np.linalg.norm(e1, axis=1)
        n = np.cross(e1, e2)
        area2 = np.linalg.norm(n, axis=1)
        self.tri_area = 0.5 * area2
        ok = (l1 > 1e-12) & (area2 > 1e-14)
        x = np.where(ok[:, None], e1 / np.maximum(l1, 1e-12)[:, None], 0.0)
        y = np.cross(n, e1)
        y = np.where(ok[:, None], y / np.maximum(np.linalg.norm(y, axis=1), 1e-12)[:, None], 0.0)
        # Coordonnées locales 2D du triangle 3D : q1 = (|e1|, 0), q2 = (e2.x, e2.y)
        q = np.zeros((len(tris), 2, 2))
        q[:, 0, 0] = l1
        q[:, 0, 1] = np.einsum("ij,ij->i", e2, x)
        q[:, 1, 1] = np.einsum("ij,ij->i", e2, y)
        det = q[:, 0, 0] * q[:, 1, 1] - q[:, 0, 1] * q[:, 1, 0]
        ok &= np.abs(det) > 1e-14
        inv = np.zeros_like(q)
        safe = np.where(ok, det, 1.0)
        inv[:, 0, 0] = q[:, 1, 1] / safe
        inv[:, 0, 1] = -q[:, 0, 1] / safe
        inv[:, 1, 0] = -q[:, 1, 0] / safe
        inv[:, 1, 1] = q[:, 0, 0] / safe
        self.tri_qinv = inv
        self.tri_ok = ok
        self.diag = float(np.linalg.norm(self.vert_co.max(0) - self.vert_co.min(0))) or 1.0

        self.compute_visibility(bm, args)
        self.compute_seam_cost(args)

    # -- visibilité : occlusion ambiante + orientation -------------------------
    def compute_visibility(self, bm, args):
        tree = getattr(args, "full_tree", None) or BVHTree.FromBMesh(bm)
        rng = np.random.default_rng(args.seed)
        n = max(4, args.ao_rays)
        # Directions de l'hémisphère, distribuées en cosinus (fixes pour tous).
        u1, u2 = rng.random(n), rng.random(n)
        r, phi = np.sqrt(u1), 2 * math.pi * u2
        local = np.stack([r * np.cos(phi), r * np.sin(phi), np.sqrt(1 - u1)], axis=1)
        max_dist = self.diag * 0.5
        eps = self.diag * 1e-4
        occ = np.zeros(self.V)
        for vi in range(self.V):
            nrm = Vector(self.vert_no[vi])
            if nrm.length < 1e-8:
                continue
            t = nrm.orthogonal().normalized()
            b = nrm.cross(t)
            origin = Vector(self.vert_co[vi]) + nrm * eps
            hits = 0
            for d in local:
                dirv = t * d[0] + b * d[1] + nrm * d[2]
                hit = tree.ray_cast(origin, dirv, max_dist)
                if hit[0] is not None:
                    hits += 1
            occ[vi] = hits / n
        front = {"-Y": (0, -1, 0), "+Y": (0, 1, 0), "-X": (-1, 0, 0), "+X": (1, 0, 0)}[args.front]
        front = np.array(front, dtype=np.float64)

        def direction_weight(nrm):
            # En jeu, le dessous d'un objet n'est presque jamais vu ; le dos
            # l'est souvent (le joueur tourne autour), il n'est que peu pénalisé.
            facing_back = np.clip(-(nrm @ front), 0, 1)
            facing_down = np.clip(-nrm[:, 2], 0, 1)
            return np.clip(1.0 - 0.3 * facing_back - 0.8 * facing_down, 0.1, 1.0)

        self.vert_vis = (1.0 - occ) * direction_weight(self.vert_no)
        face_occ = np.array([occ[vs].mean() for vs in self.face_verts])
        self.face_vis = (1.0 - face_occ) * direction_weight(self.face_no)

    # -- coût d'une couture sur chaque arête -----------------------------------
    def compute_seam_cost(self, args):
        vis = self.vert_vis[self.edge_verts].mean(1)
        ang = self.edge_angle
        sharp_rad = math.radians(args.sharp_angle)
        crease = np.clip(np.abs(ang) / sharp_rad, 0, 1)
        concave = ang < -math.radians(10)
        shape = 1.0 - 0.6 * crease
        shape = np.where(concave, shape * 0.6, shape)
        self.edge_cost = self.edge_len * (0.08 + vis) * shape + 1e-9


# ---------------------------------------------------------------------------
# Optimiseur
# ---------------------------------------------------------------------------


class UVOptimizer:
    def __init__(self, obj, args):
        self.obj = obj
        self.me = obj.data
        self.args = args
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.context.scene.tool_settings.use_uv_select_sync = True
        bpy.ops.mesh.select_mode(type="FACE")
        self.bm = bmesh.from_edit_mesh(self.me)
        self.uv_layer = self.bm.loops.layers.uv[NEW_UV_NAME]
        t = time.time()
        self.topo = Topology(self.bm, args)
        log(f"Topologie : {self.topo.V} sommets, {self.topo.F} faces, "
            f"{int(self.topo.sharp.sum())} arêtes vives ({time.time() - t:.1f} s)")
        self.seam = self.topo.mandatory.copy()
        self.uv = np.zeros((self.topo.L, 2))
        self.shape_res = 192
        self.soften_tiny_hard_islands()
        self.unwrap_method = "MINIMUM_STRETCH"
        props = bpy.ops.uv.unwrap.get_rna_type().properties
        if "MINIMUM_STRETCH" not in [e.identifier for e in props["method"].enum_items]:
            self.unwrap_method = "ANGLE_BASED"
        self.has_iterations = "iterations" in props.keys()

    def soften_tiny_hard_islands(self):
        """Un îlot minuscule entièrement entouré d'arêtes vives (petit pan,
        facette parasite) est rattaché à ses voisins : ses arêtes vives sont
        adoucies. Les arêtes non-manifold et les changements de matériau
        restent des coutures."""
        T = self.topo
        total = T.face_area.sum()
        softened = 0
        for _ in range(4):
            lab, n = self.labels()
            area = np.bincount(lab, weights=T.face_area, minlength=n)
            tiny = area < self.args.min_island * total
            if not tiny.any():
                break
            changed = 0
            for e in np.nonzero(T.sharp & self.seam)[0]:
                f0, f1 = T.edge_faces[e]
                if f0 < 0 or f1 < 0 or T.face_material[f0] != T.face_material[f1]:
                    continue
                if tiny[lab[f0]] or tiny[lab[f1]]:
                    T.sharp[e] = False
                    T.mandatory[e] = False
                    self.seam[e] = False
                    self.bm.edges[int(e)].smooth = True
                    changed += 1
            softened += changed
            if not changed:
                break
        if softened:
            bmesh.update_edit_mesh(self.me, loop_triangles=False, destructive=False)
            log(f"Arêtes vives adoucies autour de micro-îlots : {softened}")

    # -- synchronisation avec Blender ------------------------------------------
    def refresh_bm(self):
        self.bm = bmesh.from_edit_mesh(self.me)
        self.bm.faces.ensure_lookup_table()
        self.bm.edges.ensure_lookup_table()
        self.uv_layer = self.bm.loops.layers.uv[NEW_UV_NAME]

    def push_seams(self):
        for e, s in zip(self.bm.edges, self.seam):
            e.seam = bool(s)

    def read_uv(self, faces=None):
        uvl = self.uv_layer
        it = self.bm.faces if faces is None else (self.bm.faces[i] for i in faces)
        for f in it:
            for l in f.loops:
                self.uv[l.index] = l[uvl].uv

    def write_uv(self, faces=None):
        uvl = self.uv_layer
        it = self.bm.faces if faces is None else (self.bm.faces[i] for i in faces)
        for f in it:
            for l in f.loops:
                l[uvl].uv = self.uv[l.index]
        bmesh.update_edit_mesh(self.me, loop_triangles=False, destructive=False)

    def unwrap(self, faces=None):
        """Déplie toutes les faces, ou seulement `faces`, selon les coutures."""
        self.push_seams()
        sel = None if faces is None else set(int(i) for i in faces)
        for f in self.bm.faces:
            f.select_set(sel is None or f.index in sel)
        bmesh.update_edit_mesh(self.me, loop_triangles=False, destructive=False)
        kw = dict(method=self.unwrap_method, fill_holes=True, correct_aspect=True, margin=0.001)
        if self.has_iterations and self.unwrap_method == "MINIMUM_STRETCH":
            kw["iterations"] = 12
        bpy.ops.uv.unwrap(**kw)
        self.refresh_bm()
        self.read_uv(faces)

    # -- îlots (charts) ----------------------------------------------------------
    def labels(self):
        """Composantes connexes de faces à travers les arêtes sans couture."""
        T = self.topo
        lab = np.full(T.F, -1, dtype=np.int64)
        n = 0
        for start in range(T.F):
            if lab[start] >= 0:
                continue
            lab[start] = n
            dq = deque([start])
            while dq:
                f = dq.popleft()
                for e in T.face_edges[f]:
                    if self.seam[e]:
                        continue
                    for g in T.edge_faces[e]:
                        if g >= 0 and lab[g] < 0:
                            lab[g] = n
                            dq.append(g)
            n += 1
        return lab, n

    def charts(self, lab, n):
        order = np.argsort(lab, kind="stable")
        bounds = np.searchsorted(lab[order], np.arange(n + 1))
        return [order[bounds[i] : bounds[i + 1]] for i in range(n)]

    # -- topologie des îlots -------------------------------------------------------
    def chart_topology(self, lab, n):
        """Pour chaque îlot : nombre de bords (b) et caractéristique d'Euler (chi)
        de la surface découpée. Un disque a b = 1 et chi = 1."""
        T = self.topo
        seam = self.seam

        def is_border(l):
            r = T.loop_radial[l]
            if r < 0 or seam[T.loop_edge[l]]:
                return True
            return lab[T.loop_face[r]] != lab[T.loop_face[l]]

        border = np.array([is_border(l) for l in range(T.L)], dtype=bool)
        loops_per_chart = [[] for _ in range(n)]
        visited = np.zeros(T.L, dtype=bool)
        for l0 in np.nonzero(border)[0]:
            if visited[l0]:
                continue
            c = lab[T.loop_face[l0]]
            cycle = []
            l = l0
            guard = 0
            while not visited[l]:
                visited[l] = True
                cycle.append(l)
                cur = T.loop_next[l]
                inner = 0
                while not border[cur]:
                    cur = T.loop_next[T.loop_radial[cur]]
                    inner += 1
                    if inner > 10000:
                        break
                l = cur
                guard += 1
                if guard > T.L:
                    break
            loops_per_chart[c].append(cycle)

        n_border = np.bincount(lab[T.loop_face[border]], minlength=n)
        # Arêtes intérieures (comptées une fois) : coins non-bord / 2.
        n_inner_edges = np.bincount(lab[T.loop_face[~border]], minlength=n) / 2.0
        border_verts = set(zip(lab[T.loop_face[border]].tolist(), T.loop_vert[border].tolist()))
        n_inner_verts = np.zeros(n)
        seen = set()
        for f in range(T.F):
            c = lab[f]
            for v in T.face_verts[f]:
                key = (c, v)
                if key in seen or key in border_verts:
                    continue
                seen.add(key)
                n_inner_verts[c] += 1
        n_faces = np.bincount(lab, minlength=n)
        chi = (n_border + n_inner_verts) - (n_border + n_inner_edges) + n_faces
        b = np.array([len(x) for x in loops_per_chart])
        return b, np.rint(chi).astype(int), loops_per_chart

    def connect_borders(self, chart_faces, c, lab, cycles):
        """Relie le premier bord d'un îlot à un autre par le chemin le moins
        visible (Dijkstra sur le coût des coutures). Transforme un tube en
        rectangle avec une seule couture cachée."""
        T = self.topo
        src = set(T.loop_vert[cycles[0]].tolist())
        dst = set()
        for cyc in cycles[1:]:
            dst.update(T.loop_vert[cyc].tolist())
        dst -= src
        if not dst:
            return False
        in_chart = lab == c

        def usable(e):
            if self.seam[e]:
                return False
            f0, f1 = T.edge_faces[e]
            return f0 >= 0 and f1 >= 0 and in_chart[f0] and in_chart[f1]

        dist = {v: 0.0 for v in src}
        prev = {}
        heap = [(0.0, v) for v in src]
        heapq.heapify(heap)
        found = None
        while heap:
            d, v = heapq.heappop(heap)
            if d > dist.get(v, math.inf):
                continue
            if v in dst:
                found = v
                break
            for e in T.vert_edges[v]:
                if not usable(e):
                    continue
                a, b = T.edge_verts[e]
                w = b if a == v else a
                nd = d + T.edge_cost[e]
                if nd < dist.get(w, math.inf):
                    dist[w] = nd
                    prev[w] = (v, e)
                    heapq.heappush(heap, (nd, w))
        if found is None:
            return False
        v = found
        while v in prev:
            pv, e = prev[v]
            self.seam[e] = True
            v = pv
        return True

    def cut_to_disk(self, faces, c, lab):
        """Découpe minimale qui transforme un îlot à bords (tube, anse) en
        disque : arbre couvrant maximal du graphe dual (on garde les arêtes
        coûteuses, c.-à-d. visibles), puis élagage des coupures pendantes.
        Un tube reçoit une seule couture, du côté le moins visible."""
        T = self.topo
        in_chart = lab == c
        cand = set()
        for f in faces:
            for e in T.face_edges[f]:
                if self.seam[e]:
                    continue
                f0, f1 = T.edge_faces[e]
                if f0 >= 0 and f1 >= 0 and in_chart[f0] and in_chart[f1]:
                    cand.add(int(e))
        parent = {int(f): int(f) for f in faces}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        cut = []
        for e in sorted(cand, key=lambda e: -T.edge_cost[e]):
            a, b = find(int(T.edge_faces[e][0])), find(int(T.edge_faces[e][1]))
            if a != b:
                parent[a] = b
            else:
                cut.append(e)
        # Sommets ancrés : ceux déjà sur un bord de l'îlot.
        anchors = set()
        for f in faces:
            for e in T.face_edges[f]:
                f0, f1 = T.edge_faces[e]
                if self.seam[e] or f0 < 0 or f1 < 0 or not (in_chart[f0] and in_chart[f1]):
                    anchors.update(T.edge_verts[e].tolist())
        deg = {}
        inc = {}
        for e in cut:
            for v in T.edge_verts[e]:
                v = int(v)
                deg[v] = deg.get(v, 0) + 1
                inc.setdefault(v, set()).add(e)
        alive = set(cut)
        stack = [v for v, d in deg.items() if d == 1 and v not in anchors]
        while stack:
            v = stack.pop()
            if deg.get(v, 0) != 1 or v in anchors:
                continue
            e = next(iter(inc[v] & alive))
            alive.discard(e)
            for w in T.edge_verts[e]:
                w = int(w)
                deg[w] -= 1
                inc[w].discard(e)
                if deg[w] == 1 and w not in anchors:
                    stack.append(w)
        for e in alive:
            self.seam[e] = True
        return bool(alive)

    def split_chart(self, faces, evaluate=False):
        """Coupe un îlot en deux. Plusieurs découpes candidates sont essayées
        (regroupement par normales, plans selon les axes principaux et les
        axes du monde) ; chacune est affinée pour suivre les arêtes les moins
        coûteuses, puis notée : coût de la couture, équilibre des deux parts
        et, si `evaluate`, distorsion réelle après un dépliage d'essai."""
        T = self.topo
        faces = np.asarray(faces)
        if len(faces) < 2:
            return False
        nrm = T.face_no[faces]
        area = T.face_area[faces] + 1e-12
        rel_area = area / area.mean()
        cands = []

        # Candidat 1 : k-moyennes sur les normales (k = 2).
        mean = (nrm * area[:, None]).sum(0)
        a = int(np.argmin(nrm @ (mean / (np.linalg.norm(mean) + 1e-12))))
        b = int(np.argmin(nrm @ nrm[a]))
        centers = np.stack([nrm[a], nrm[b]])
        if np.dot(centers[0], centers[1]) < 0.97:
            for _ in range(10):
                label = np.argmax(nrm @ centers.T, axis=1)
                new = []
                for k in range(2):
                    m = label == k
                    if not m.any():
                        new.append(centers[k])
                        continue
                    v = (nrm[m] * area[m, None]).sum(0)
                    new.append(v / (np.linalg.norm(v) + 1e-12))
                centers = np.stack(new)
            data = np.stack([(1.0 - nrm @ centers[k]) * rel_area for k in range(2)], 1)
            cands.append(self._refine(faces, np.argmax(nrm @ centers.T, axis=1), data))

        # Candidats 2+ : plans passant par le centre, perpendiculaires aux
        # axes principaux de l'îlot et aux axes du monde.
        pts = T.face_center[faces]
        center = (pts * area[:, None]).sum(0) / area.sum()
        rel = pts - center
        axes = []
        try:
            _, _, vt = np.linalg.svd(rel * np.sqrt(area)[:, None], full_matrices=False)
            axes += [vt[i] for i in range(min(3, len(vt)))]
        except np.linalg.LinAlgError:
            pass
        axes += [np.array(v, dtype=float) for v in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
        uniq = []
        for ax in axes:
            ax = ax / (np.linalg.norm(ax) + 1e-12)
            if all(abs(float(ax @ u)) < 0.98 for u in uniq):
                uniq.append(ax)
        for ax in uniq:
            d = rel @ ax
            ext = float(np.abs(d).max()) or 1.0
            dn = d / ext
            data = np.stack([np.maximum(0, dn) * 4 * rel_area, np.maximum(0, -dn) * 4 * rel_area], 1)
            cands.append(self._refine(faces, (dn > 0).astype(int), data))

        best, best_score = None, math.inf
        tot_area = area.sum()
        edges_in = {e for f in faces for e in T.face_edges[f]}
        cpl = np.mean([T.edge_cost[e] / max(T.edge_len[e], 1e-12) for e in edges_in]) or 1.0
        for label in cands:
            if label.min() == label.max():
                continue
            a1 = area[label == 1].sum() / tot_area
            if min(a1, 1 - a1) < 0.04:
                continue
            boundary = self._boundary_edges(faces, label)
            if not boundary:
                continue
            bcost = sum(T.edge_cost[e] for e in boundary) / (cpl * math.sqrt(tot_area))
            score = bcost + 2.0 * abs(1 - 2 * a1)
            if evaluate:
                score += 4.0 * self._trial_distortion(faces, boundary)
            if score < best_score:
                best, best_score = boundary, score
        if best is None:
            label = self._spatial_split(faces)
            if label.min() == label.max():
                return False
            best = self._boundary_edges(faces, label)
            if not best:
                return False
        for e in best:
            self.seam[e] = True
        return True

    def _boundary_edges(self, faces, label):
        T = self.topo
        local = {int(f): int(k) for f, k in zip(faces, label)}
        out = []
        for f in faces:
            for e in T.face_edges[f]:
                if self.seam[e]:
                    continue
                g0, g1 = T.edge_faces[e]
                if g0 in local and g1 in local and local[g0] != local[g1]:
                    out.append(int(e))
        return sorted(set(out))

    def _trial_distortion(self, faces, boundary):
        """Dépliage d'essai avec la découpe proposée ; renvoie la pire
        distorsion relative (1.0 = au seuil). L'état est ensuite restauré."""
        T = self.topo
        loops = np.concatenate([np.arange(T.face_loop_start[f], T.face_loop_start[f + 1]) for f in faces])
        saved_uv = self.uv[loops].copy()
        for e in boundary:
            self.seam[e] = True
        lab, n = self.labels()
        self.unwrap(faces)
        mean, p95, flips, _ = self.chart_stats(lab, n)
        ids = np.unique(lab[faces])
        comp, overlap = self.chart_shape(lab, n, ids)
        a = self.args
        worst = max(
            max(mean[ids].max() / a.max_distortion, p95[ids].max() / a.max_distortion_p95),
            3.0 if flips[ids].sum() > 0 or overlap[ids].any() else 0.0,
        )
        worst += 2.0 * max(0.0, (a.min_compactness - comp[ids].min()) / a.min_compactness)
        for e in boundary:
            self.seam[e] = False
        self.uv[loops] = saved_uv
        self.write_uv(faces)
        return float(worst)

    def _spatial_split(self, faces):
        T = self.topo
        pts = T.face_center[faces]
        pts = pts - pts.mean(0)
        _, _, vt = np.linalg.svd(pts, full_matrices=False)
        proj = pts @ vt[0]
        return (proj > np.median(proj)).astype(int)

    def _refine(self, faces, label, data, passes=8):
        """Affinage de type ICM : chaque face prend l'étiquette qui minimise
        son coût propre (`data`) + le coût des coutures avec ses voisines.
        La frontière glisse ainsi vers les arêtes cachées et les plis."""
        T = self.topo
        idx = {int(f): i for i, f in enumerate(faces)}
        label = label.copy()
        scale = np.median(T.edge_cost) * 4 + 1e-12
        nbrs = []
        for f in faces:
            lst = []
            for e in T.face_edges[f]:
                if self.seam[e]:
                    continue
                for g in T.edge_faces[e]:
                    if g >= 0 and g != f and int(g) in idx:
                        lst.append((idx[int(g)], T.edge_cost[e] / scale))
            nbrs.append(lst)
        for _ in range(passes):
            changed = 0
            for i in range(len(faces)):
                cost = [data[i, 0], data[i, 1]]
                for j, w in nbrs[i]:
                    cost[1 - label[j]] += w
                best = 0 if cost[0] <= cost[1] else 1
                if best != label[i]:
                    label[i] = best
                    changed += 1
            if not changed:
                break
        return label

    def ensure_disks(self, max_passes=60):
        """Coupe jusqu'à ce que chaque îlot soit topologiquement un disque."""
        for _ in range(max_passes):
            lab, n = self.labels()
            b, chi, cycles = self.chart_topology(lab, n)
            bad = np.nonzero(~((b == 1) & (chi == 1)))[0]
            if len(bad) == 0:
                return lab, n
            charts = self.charts(lab, n)
            for c in bad:
                genus2 = 2 - chi[c] - b[c]  # = 2 * genre
                if b[c] == 0:
                    # Forme fermée (rotule, capot) : coupe en deux.
                    self.split_chart(charts[c])
                elif b[c] >= 2 and genus2 == 0:
                    # Tube ou anneau : une couture droite et cachée entre deux
                    # bords (plus court chemin pondéré par la visibilité).
                    if not self.connect_borders(charts[c], c, lab, cycles[c]):
                        if not self.cut_to_disk(charts[c], c, lab):
                            self.split_chart(charts[c])
                elif not self.cut_to_disk(charts[c], c, lab):
                    self.split_chart(charts[c])
        lab, n = self.labels()
        return lab, n

    # -- mesure de distorsion ---------------------------------------------------
    def tri_distortion(self, lab):
        """Distorsion par triangle : max(|log(s1/s)|, |log(s2/s)|), où s1, s2
        sont les valeurs singulières de l'application 3D -> UV et s l'échelle
        moyenne de l'îlot. Combine étirement et cisaillement."""
        T = self.topo
        uv = self.uv[T.tri_loops]
        d1 = uv[:, 1] - uv[:, 0]
        d2 = uv[:, 2] - uv[:, 0]
        D = np.stack([np.stack([d1[:, 0], d2[:, 0]], 1), np.stack([d1[:, 1], d2[:, 1]], 1)], 1)
        J = D @ T.tri_qinv
        a, b, c, d = J[:, 0, 0], J[:, 0, 1], J[:, 1, 0], J[:, 1, 1]
        det = a * d - b * c
        S = a * a + b * b + c * c + d * d
        disc = np.sqrt(np.maximum(S * S - 4 * det * det, 0))
        s1 = np.sqrt(np.maximum((S + disc) / 2, 0))
        s2 = np.sqrt(np.maximum((S - disc) / 2, 0))
        tri_lab = lab[T.tri_face]
        n = lab.max() + 1
        w = np.where(T.tri_ok, T.tri_area, 0.0)
        # det(J) = aire UV / aire 3D (signée)
        signed_uv = det * T.tri_area
        sum3 = np.bincount(tri_lab, weights=w, minlength=n)
        sumuv = np.bincount(tri_lab, weights=np.abs(signed_uv) * T.tri_ok, minlength=n)
        sgn = np.sign(np.bincount(tri_lab, weights=signed_uv * T.tri_ok, minlength=n))
        scale = np.sqrt(sumuv / np.maximum(sum3, 1e-20))
        s = scale[tri_lab]
        with np.errstate(divide="ignore", invalid="ignore"):
            dist = np.maximum(np.abs(np.log(s1 / s)), np.abs(np.log(s2 / s)))
        dist = np.where(np.isfinite(dist), dist, 3.0)
        dist = np.minimum(dist, 3.0)
        flipped = (det * sgn[tri_lab] < 0) & T.tri_ok & (np.abs(det) > 1e-12 * (s * s + 1e-30))
        dist = np.where(flipped, 3.0, dist)
        dist = np.where(T.tri_ok, dist, 0.0)
        return dist, w, flipped, scale

    def chart_stats(self, lab, n):
        dist, w, flipped, scale = self.tri_distortion(lab)
        tri_lab = lab[self.topo.tri_face]
        sumw = np.bincount(tri_lab, weights=w, minlength=n)
        mean = np.bincount(tri_lab, weights=dist * w, minlength=n) / np.maximum(sumw, 1e-20)
        p95 = np.zeros(n)
        order = np.lexsort((dist, tri_lab))
        sl, sd, sw = tri_lab[order], dist[order], w[order]
        bounds = np.searchsorted(sl, np.arange(n + 1))
        for c in range(n):
            a, b = bounds[c], bounds[c + 1]
            if b <= a or sumw[c] <= 0:
                continue
            cw = np.cumsum(sw[a:b])
            k = np.searchsorted(cw, 0.95 * cw[-1])
            p95[c] = sd[a + min(k, b - a - 1)]
        flips = np.bincount(tri_lab, weights=flipped.astype(float), minlength=n)
        return mean, p95, flips, scale

    def chart_shape(self, lab, n, ids=None, res=None):
        """Compacité (aire UV / aire de l'enveloppe convexe) et
        auto-chevauchement de chaque îlot, mesurés dans son propre repère UV.
        Un îlot peu compact (bras fins, formes en Y) gaspille la texture au
        rangement ; un îlot qui se chevauche donnerait deux zones du mesh
        sur les mêmes pixels."""
        T = self.topo
        res = res or self.shape_res
        comp = np.ones(n)
        overlap = np.zeros(n, dtype=bool)
        order = np.argsort(lab[T.tri_face], kind="stable")
        tl = lab[T.tri_face][order]
        bounds = np.searchsorted(tl, np.arange(n + 1))
        todo = range(n) if ids is None else ids
        for c in todo:
            tris = order[bounds[c] : bounds[c + 1]]
            if len(tris) == 0:
                continue
            uv = self.uv[T.tri_loops[tris]]
            pts = uv.reshape(-1, 2)
            hull = convex_hull(pts)
            if len(hull) < 3:
                continue
            hx, hy = hull[:, 0], hull[:, 1]
            hull_area = 0.5 * abs(np.dot(hx, np.roll(hy, 1)) - np.dot(hy, np.roll(hx, 1)))
            d1 = uv[:, 1] - uv[:, 0]
            d2 = uv[:, 2] - uv[:, 0]
            uv_area = 0.5 * np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]).sum()
            comp[c] = uv_area / max(hull_area, 1e-20)
            if len(tris) > 1:
                lo = pts.min(0)
                ext = float((pts.max(0) - lo).max()) or 1.0
                cover = raster_count((uv - lo) / ext * (res - 1), res)
                overlap[c] = (cover > 1).sum() > max(2, 0.0005 * (cover > 0).sum())
        return comp, overlap

    def is_bad(self, mean, p95, flips, strict=1.0, area_frac=None):
        a = self.args
        tol = np.ones_like(mean) * strict
        if area_frac is not None:
            # Un petit îlot (vis, écrou) tolère plus de distorsion : la
            # recouper ne ferait qu'ajouter des coutures visibles.
            tol = tol * np.where(area_frac < 0.003, 2.0, 1.0)
        return (mean > a.max_distortion * tol) | (p95 > a.max_distortion_p95 * tol) | (flips > 0)

    # -- étapes principales ------------------------------------------------------
    def segment(self):
        t = time.time()
        a = self.args
        for it in range(a.max_iterations):
            lab, n = self.ensure_disks()
            self.unwrap()
            mean, p95, flips, _ = self.chart_stats(lab, n)
            area = np.bincount(lab, weights=self.topo.face_area, minlength=n)
            frac = area / area.sum()
            nfaces = np.bincount(lab, minlength=n)
            comp, overlap = self.chart_shape(lab, n)
            distorted = self.is_bad(mean, p95, flips, area_frac=frac) & (nfaces >= 3)
            sprawling = (comp < a.min_compactness) & (frac > 0.0005) & (nfaces >= 6)
            bad = np.nonzero(distorted | sprawling | overlap)[0]
            log(f"Itération {it + 1} : {n} îlots - {int(distorted.sum())} trop déformés, "
                f"{int(sprawling.sum())} trop étalés, {int(overlap.sum())} repliés "
                f"(distorsion moyenne {np.average(mean, weights=area + 1e-12):.3f}, "
                f"compacité moyenne {np.average(comp, weights=area + 1e-12):.2f})")
            if len(bad) == 0:
                break
            charts = self.charts(lab, n)
            progress = False
            for c in bad:
                progress |= self.split_chart(charts[c], evaluate=len(charts[c]) <= 4000)
            if not progress:
                log("Plus de découpe possible sur les îlots restants.")
                break
        log(f"Segmentation terminée en {time.time() - t:.1f} s")

    def merge_small(self):
        """Recolle les îlots voisins séparés par des coutures non obligatoires,
        en commençant par les plus petits, si le résultat reste un disque peu
        déformé."""
        T = self.topo
        t = time.time()
        merged = 0
        rejected = set()
        for _round in range(4):
            lab, n = self.labels()
            charts = self.charts(lab, n)
            area = np.bincount(lab, weights=T.face_area, minlength=n)
            # Arêtes de couture souples entre deux îlots différents.
            pairs = {}
            for e in np.nonzero(self.seam & ~T.mandatory)[0]:
                f0, f1 = T.edge_faces[e]
                if f0 < 0 or f1 < 0:
                    continue
                a, b = lab[f0], lab[f1]
                if a == b:
                    continue
                key = (min(a, b), max(a, b))
                pairs.setdefault(key, []).append(e)
            cand = sorted(pairs.items(), key=lambda kv: (min(area[kv[0][0]], area[kv[0][1]]), -T.edge_len[kv[1]].sum()))
            touched = set()
            round_merged = 0
            for (a, b), edges in cand:
                if a in touched or b in touched:
                    continue
                sig = (frozenset(charts[a].tolist()), frozenset(charts[b].tolist()))
                if sig in rejected:
                    continue
                faces = np.concatenate([charts[a], charts[b]])
                saved_uv = {l: self.uv[l].copy() for f in faces for l in range(T.face_loop_start[f], T.face_loop_start[f + 1])}
                saved_seam = self.seam[edges].copy()
                self.seam[edges] = False
                lab2, n2 = self.labels()
                c = lab2[faces[0]]
                ok = bool(np.all(lab2[faces] == c))
                if ok:
                    b2, chi2, _ = self.chart_topology(lab2, n2)
                    ok = b2[c] == 1 and chi2[c] == 1
                if ok:
                    self.unwrap(faces)
                    mean, p95, flips, _ = self.chart_stats(lab2, n2)
                    frac = np.array([T.face_area[faces].sum() / T.face_area.sum()])
                    ok = not self.is_bad(mean[c : c + 1], p95[c : c + 1], flips[c : c + 1], strict=0.9, area_frac=frac)[0]
                if ok:
                    comp, overlap = self.chart_shape(lab2, n2, [c])
                    ok = not overlap[c] and (comp[c] >= self.args.min_compactness or frac[0] < 0.0005)
                if ok:
                    touched.update((a, b))
                    merged += 1
                    round_merged += 1
                else:
                    self.seam[edges] = saved_seam
                    for l, v in saved_uv.items():
                        self.uv[l] = v
                    self.write_uv(faces)
                    rejected.add(sig)
            if round_merged == 0:
                break
        lab, n = self.labels()
        log(f"Fusion : {merged} îlots recollés, {n} îlots restants ({time.time() - t:.1f} s)")

    def smooth_boundaries(self, passes=3):
        """Lissage des frontières entre îlots : une face de bordure qui touche
        davantage un îlot voisin que le sien y est transférée (jamais à
        travers une couture obligatoire). Supprime les dents de scie qui
        gaspillent de la place au rangement et allongent les coutures."""
        T = self.topo
        lab, n = self.labels()
        slit = self.seam & ~T.mandatory
        slit &= np.array([f0 >= 0 and f1 >= 0 and lab[f0] == lab[f1] for f0, f1 in T.edge_faces])
        moved_total = 0
        for _ in range(passes):
            moved = 0
            new = lab.copy()
            for f in range(T.F):
                counts = {}
                for e in T.face_edges[f]:
                    if T.mandatory[e] or slit[e]:
                        continue
                    for g in T.edge_faces[e]:
                        if g >= 0 and g != f:
                            counts[lab[g]] = counts.get(lab[g], 0) + 1
                own = counts.get(lab[f], 0)
                best = max(counts.items(), key=lambda kv: kv[1], default=(lab[f], 0))
                if best[0] != lab[f] and best[1] > own and best[1] >= 2:
                    new[f] = best[0]
                    moved += 1
            lab = new
            moved_total += moved
            if not moved:
                break
        if moved_total:
            for e in range(T.E):
                f0, f1 = T.edge_faces[e]
                between = f0 >= 0 and f1 >= 0 and lab[f0] != lab[f1]
                self.seam[e] = T.mandatory[e] or slit[e] or between
        log(f"Lissage des frontières : {moved_total} faces réattribuées")

    def detach_thin_parts(self, res=160, radius=2):
        """Repère, par ouverture morphologique dans l'espace UV, les bandes
        étroites accrochées aux îlots (« moustaches », bandeaux de chanfrein)
        et les détache : redressées ensuite en rectangles, elles se rangent
        dans les interstices au lieu d'agrandir l'encombrement de l'îlot."""
        T = self.topo
        lab, n = self.labels()
        charts = self.charts(lab, n)
        total = T.face_area.sum()
        detached = 0
        for c, faces in enumerate(charts):
            if len(faces) < 8 or T.face_area[faces].sum() < 0.002 * total:
                continue
            tris = np.nonzero(np.isin(T.tri_face, faces))[0]
            uv = self.uv[T.tri_loops[tris]]
            lo = uv.reshape(-1, 2).min(0)
            ext = float((uv.reshape(-1, 2).max(0) - lo).max()) or 1.0
            px = (uv - lo) / ext * (res - 1)
            mask = raster_count(px, res) > 0
            opened = dilate(erode(mask, radius), radius)
            thin_px = mask & ~opened
            if thin_px.sum() < 0.03 * mask.sum():
                continue
            cen = px.mean(1)
            ix = np.clip(cen[:, 0].astype(int), 0, res - 1)
            iy = np.clip(cen[:, 1].astype(int), 0, res - 1)
            tri_thin = thin_px[iy, ix]
            thin_faces = set(T.tri_face[tris][tri_thin].tolist())
            # Une face n'est « fine » que si tous ses triangles le sont.
            thin_faces -= set(T.tri_face[tris][~tri_thin].tolist())
            if not thin_faces or T.face_area[list(thin_faces)].sum() < 0.01 * T.face_area[faces].sum():
                continue
            for f in thin_faces:
                for e in T.face_edges[f]:
                    f0, f1 = T.edge_faces[e]
                    if f0 < 0 or f1 < 0:
                        continue
                    if (f0 in thin_faces) != (f1 in thin_faces) and lab[f0] == lab[f1] == c:
                        self.seam[e] = True
            detached += 1
        log(f"Parties fines détachées sur {detached} îlots")

    def split_elongated(self, max_rel_length=1.0, rounds=4):
        """Un îlot très long limite l'échelle de tout le rangement (il doit
        tenir dans la largeur de la texture). S'il est plus long que la
        racine de la surface totale, il est coupé en travers de sa longueur,
        sur les arêtes les moins visibles : une couture de plus, mais plus de
        résolution pour tout l'objet."""
        T = self.topo
        total = math.sqrt(T.face_area.sum())
        cut = 0
        for _ in range(rounds):
            lab, n = self.labels()
            charts = self.charts(lab, n)
            _, _, _, scale = self.chart_stats(lab, n)
            done = False
            for c, faces in enumerate(charts):
                if len(faces) < 4 or scale[c] <= 0:
                    continue
                loops = np.concatenate([np.arange(T.face_loop_start[f], T.face_loop_start[f + 1]) for f in faces])
                pts = self.uv[loops] / scale[c]
                theta = min_area_rect_angle(pts)
                cs, sn = math.cos(-theta), math.sin(-theta)
                r = pts @ np.array([[cs, -sn], [sn, cs]]).T
                ext = r.max(0) - r.min(0)
                if ext.max() / total <= max_rel_length:
                    continue
                if self._split_across(faces):
                    cut += 1
                    done = True
            if not done:
                break
            self.ensure_disks()
            self.unwrap()
        if cut:
            log(f"Îlots trop longs coupés en travers : {cut}")

    def _split_across(self, faces):
        """Coupe un îlot par un plan perpendiculaire à sa plus grande
        dimension, au milieu, puis fait glisser la coupe vers les arêtes les
        moins coûteuses."""
        T = self.topo
        faces = np.asarray(faces)
        area = T.face_area[faces] + 1e-12
        pts = T.face_center[faces]
        center = (pts * area[:, None]).sum(0) / area.sum()
        rel = pts - center
        _, _, vt = np.linalg.svd(rel * np.sqrt(area)[:, None], full_matrices=False)
        d = rel @ vt[0]
        dn = d / (float(np.abs(d).max()) or 1.0)
        rel_area = area / area.mean()
        data = np.stack([np.maximum(0, dn) * 4 * rel_area, np.maximum(0, -dn) * 4 * rel_area], 1)
        label = self._refine(faces, (dn > 0).astype(int), data)
        if label.min() == label.max():
            return False
        boundary = self._boundary_edges(faces, label)
        for e in boundary:
            self.seam[e] = True
        return bool(boundary)

    def detach_faces(self, faces):
        """Détache des faces de leur îlot (couture tout autour)."""
        T = self.topo
        for f in faces:
            for e in T.face_edges[f]:
                self.seam[e] = True

    def straighten(self):
        """Redresse les îlots entièrement en quads (grilles) en rectangles."""
        T = self.topo
        lab, n = self.labels()
        charts = self.charts(lab, n)
        mean0, p950, flips0, _ = self.chart_stats(lab, n)
        done = 0
        for c, faces in enumerate(charts):
            if len(faces) < 2 or not T.face_is_quad[faces].all():
                continue
            saved = {l: self.uv[l].copy() for f in faces for l in range(T.face_loop_start[f], T.face_loop_start[f + 1])}
            active = self._best_quad(faces)
            self._set_rect_uv(active)
            self.push_seams()
            for f in self.bm.faces:
                f.select_set(False)
            for f in faces:
                self.bm.faces[int(f)].select_set(True)
            self.bm.faces.active = self.bm.faces[int(active)]
            bmesh.update_edit_mesh(self.me, loop_triangles=False, destructive=False)
            try:
                bpy.ops.uv.follow_active_quads(mode="LENGTH_AVERAGE")
            except RuntimeError:
                pass
            self.refresh_bm()
            self.read_uv(faces)
            mean, p95, flips, _ = self.chart_stats(lab, n)
            limit = max(mean0[c] + 0.03, self.args.max_distortion)
            ok = mean[c] <= limit and p95[c] <= self.args.max_distortion_p95 and flips[c] == 0
            if ok:
                _, overlap = self.chart_shape(lab, n, [c])
                ok = not overlap[c]
            if ok:
                done += 1
            else:
                for l, v in saved.items():
                    self.uv[l] = v
                self.write_uv(faces)
        log(f"Redressement : {done} îlots en grille redressés")

    def _best_quad(self, faces):
        T = self.topo
        center = T.face_center[faces].mean(0)
        best, best_score = faces[0], -1.0
        for f in faces:
            vs = T.vert_co[T.face_verts[f]]
            e = [np.linalg.norm(vs[(i + 1) % 4] - vs[i]) for i in range(4)]
            rect = min(e[0], e[2]) / max(e[0], e[2], 1e-12) * min(e[1], e[3]) / max(e[1], e[3], 1e-12)
            d = np.linalg.norm(T.face_center[f] - center) / T.diag
            score = rect * (1.0 - min(d, 0.9))
            if score > best_score:
                best, best_score = f, score
        return int(best)

    def _set_rect_uv(self, f):
        T = self.topo
        vs = T.vert_co[T.face_verts[f]]
        w = 0.5 * (np.linalg.norm(vs[1] - vs[0]) + np.linalg.norm(vs[2] - vs[3]))
        h = 0.5 * (np.linalg.norm(vs[3] - vs[0]) + np.linalg.norm(vs[2] - vs[1]))
        corners = [(0, 0), (w, 0), (w, h), (0, h)]
        face = self.bm.faces[f]
        for l, c in zip(face.loops, corners):
            l[self.uv_layer].uv = c

    def layout(self):
        """Échelle (densité de texels), orientation et packing final."""
        T = self.topo
        a = self.args
        lab, n = self.labels()
        charts = self.charts(lab, n)
        _, _, _, scale = self.chart_stats(lab, n)
        area = np.bincount(lab, weights=T.face_area, minlength=n)
        vis = np.bincount(lab, weights=T.face_vis * T.face_area, minlength=n) / np.maximum(area, 1e-20)
        hd = float(np.clip(a.hidden_density, 0.05, 1.0))
        tvis = np.clip((vis - 0.25) / (0.6 - 0.25), 0, 1)
        tvis = tvis * tvis * (3 - 2 * tvis)
        density = hd + (1 - hd) * tvis
        for c, faces in enumerate(charts):
            loops = np.concatenate([np.arange(T.face_loop_start[f], T.face_loop_start[f + 1]) for f in faces])
            pts = self.uv[loops]
            if scale[c] <= 0 or not np.isfinite(scale[c]):
                continue
            pts = (pts - pts.mean(0)) * (density[c] / scale[c])
            theta = min_area_rect_angle(pts)
            cs, sn = math.cos(-theta), math.sin(-theta)
            R = np.array([[cs, -sn], [sn, cs]])
            pts = pts @ R.T
            self.uv[loops] = pts
        self.write_uv()
        self.chart_density_factor = density
        self.chart_visibility = vis

        t = time.time()
        islands = []
        for faces in charts:
            loops = np.concatenate([np.arange(T.face_loop_start[f], T.face_loop_start[f + 1]) for f in faces])
            tris = np.nonzero(np.isin(T.tri_face, faces))[0]
            islands.append((loops, T.tri_loops[tris]))
        placed = raster_pack(self.uv, islands, a.texture_size, a.padding)
        if placed is not None:
            self.uv = placed
            self.write_uv()
            log(f"Rangement par forme réelle : {time.time() - t:.1f} s")
            return
        log("Rangement par forme réelle impossible, repli sur le rangement de Blender")
        for f in self.bm.faces:
            f.select_set(True)
        bmesh.update_edit_mesh(self.me, loop_triangles=False, destructive=False)
        bpy.ops.uv.pack_islands(
            udim_source="CLOSEST_UDIM",
            rotate=True,
            rotate_method="CARDINAL",
            scale=True,
            merge_overlap=False,
            margin_method="FRACTION",
            margin=a.padding / float(a.texture_size),
            shape_method="CONCAVE",
        )
        self.refresh_bm()
        self.read_uv()

    # -- contrôle final ---------------------------------------------------------
    def rasterize(self, lab, res):
        """Rastérise les triangles UV : renvoie (couverture, îlot dominant,
        pixels en chevauchement, îlots impliqués dans un chevauchement)."""
        T = self.topo
        cover = np.zeros((res, res), dtype=np.int32)
        owner = np.full((res, res), -1, dtype=np.int64)
        owner_tri = np.full((res, res), -1, dtype=np.int64)
        overlap_pairs = set()
        self.overlap_tris = set()
        uv = self.uv[T.tri_loops] * res
        tri_lab = lab[T.tri_face]
        for t in range(len(uv)):
            p = uv[t]
            x0, y0 = np.floor(p.min(0)).astype(int)
            x1, y1 = np.ceil(p.max(0)).astype(int)
            x0, y0 = max(x0, 0), max(y0, 0)
            x1, y1 = min(x1, res), min(y1, res)
            if x1 <= x0 or y1 <= y0:
                continue
            xs = np.arange(x0, x1) + 0.5
            ys = np.arange(y0, y1) + 0.5
            X, Y = np.meshgrid(xs, ys)
            (ax, ay), (bx, by), (cx, cy) = p
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-12:
                continue
            l1 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) / d
            l2 = ((cy - ay) * (X - cx) + (ax - cx) * (Y - cy)) / d
            l3 = 1 - l1 - l2
            # Marge négative : on ignore les pixels à cheval sur une arête
            # partagée entre deux triangles voisins.
            inside = (l1 > 1e-4) & (l2 > 1e-4) & (l3 > 1e-4)
            sub_owner = owner[y0:y1, x0:x1]
            c = tri_lab[t]
            # Pour l'image et le taux de remplissage, les pixels sur les
            # arêtes comptent aussi.
            touch = (l1 > -1e-3) & (l2 > -1e-3) & (l3 > -1e-3) & (sub_owner < 0)
            sub_owner[touch] = c
            if not inside.any():
                continue
            clash = inside & (sub_owner >= 0) & (cover[y0:y1, x0:x1] > 0)
            sub_tri = owner_tri[y0:y1, x0:x1]
            if clash.any():
                for o in np.unique(sub_owner[clash]):
                    overlap_pairs.add((int(o), int(c)))
                self.overlap_tris.add(int(t))
                self.overlap_tris.update(int(x) for x in np.unique(sub_tri[clash]) if x >= 0)
            cover[y0:y1, x0:x1] += inside
            sub_owner[inside] = c
            sub_tri[inside] = t
        return cover, owner, overlap_pairs

    def report(self, elapsed):
        T = self.topo
        a = self.args
        lab, n = self.labels()
        mean, p95, flips, scale = self.chart_stats(lab, n)
        area = np.bincount(lab, weights=T.face_area, minlength=n)
        res = 1024
        cover, owner, pairs = self.rasterize(lab, res)
        self_overlap = sorted({p[0] for p in pairs if p[0] == p[1]})
        cross_overlap = sorted({p for p in pairs if p[0] != p[1]})
        px_per_m = scale * a.texture_size
        density_norm = px_per_m / np.maximum(self.chart_density_factor, 1e-6)
        dist_tri, w, _, _ = self.tri_distortion(lab)
        ok_w = w > 0
        weighted = lambda x: float(np.average(x, weights=area + 1e-20))
        rep = {
            "input": os.path.abspath(a.input),
            "output": os.path.abspath(a.output),
            "texture_size": a.texture_size,
            "padding_px": a.padding,
            "faces": int(T.F),
            "islands": int(n),
            "seam_edges": int(self.seam.sum()),
            "hard_edges": int(T.sharp.sum()),
            "hard_edges_not_seam": int((T.sharp & ~self.seam).sum()),
            "uv_coverage_percent": round(100.0 * float((owner >= 0).mean()), 2),
            "overlap_pixels_percent": round(100.0 * float((cover > 1).mean()), 4),
            "islands_self_overlapping": [int(x) for x in self_overlap],
            "island_pairs_overlapping": [[int(x), int(y)] for x, y in cross_overlap],
            "flipped_triangles": int(flips.sum()),
            "distortion_mean": round(weighted(mean), 4),
            "distortion_p95_worst_island": round(float(p95.max()), 4) if n else 0.0,
            "distortion_area_over_10pct_percent": round(100.0 * float(w[ok_w & (dist_tri > 0.10)].sum() / max(w.sum(), 1e-20)), 2),
            "texel_density_px_per_m": {
                "median": round(float(np.median(px_per_m)), 1),
                "min": round(float(px_per_m.min()), 1),
                "max": round(float(px_per_m.max()), 1),
                "uniformity_percent": round(100.0 * float(density_norm.min() / max(density_norm.max(), 1e-12)), 1),
            },
            "seconds": round(elapsed, 1),
        }
        status = "OK"
        warnings = []
        if rep["hard_edges_not_seam"]:
            warnings.append("Des arêtes vives ne sont pas des coutures.")
        if self_overlap or cross_overlap:
            warnings.append("Chevauchements UV détectés.")
        if rep["flipped_triangles"]:
            warnings.append("Triangles retournés détectés.")
        if rep["distortion_mean"] > a.max_distortion:
            warnings.append("Distorsion moyenne au-dessus du seuil.")
        if warnings:
            status = "ATTENTION"
        rep["status"] = status
        rep["warnings"] = warnings
        return rep, cover, owner


def conservative_mask(tri_px, h, w):
    """Masque de toutes les cellules touchées par les triangles (centres
    couverts + points échantillonnés le long des arêtes) : aucune partie
    d'un îlot, même un triangle plus fin qu'une cellule, n'est oubliée."""
    mask = raster_count(tri_px, max(h, w))[:h, :w] > 0
    edges = np.concatenate([tri_px[:, [0, 1]], tri_px[:, [1, 2]], tri_px[:, [2, 0]]])
    length = np.linalg.norm(edges[:, 1] - edges[:, 0], axis=1)
    k = int(max(2, np.ceil(length.max() * 2) + 1)) if len(length) else 2
    tt = np.linspace(0, 1, k)
    pts = edges[:, 0, None, :] + (edges[:, 1] - edges[:, 0])[:, None, :] * tt[None, :, None]
    ix = np.clip(np.floor(pts[..., 0]).astype(int), 0, w - 1).ravel()
    iy = np.clip(np.floor(pts[..., 1]).astype(int), 0, h - 1).ravel()
    mask[iy, ix] = True
    return mask


def raster_pack(uv, islands, texture_size, padding_px, fill_lo=0.25):
    """Rangement des îlots selon leur forme réelle (principe de xatlas).

    Chaque îlot est rastérisé sur une grille ; il est placé, à 0° ou 90°, à
    la position libre la plus proche du coin (les collisions pour toutes les
    positions sont calculées d'un coup par corrélation FFT). L'échelle
    globale est la plus grande pour laquelle tout rentre (dichotomie).
    Renvoie les nouveaux UV, ou None en cas d'échec."""
    G = int(min(1024, max(256, texture_size // 2)))
    cell_px = texture_size / G
    r = max(1, int(math.ceil(padding_px / (2.0 * cell_px))))
    shapes = []
    total = 0.0
    for loops, tri_loops in islands:
        pts = uv[loops]
        tri = uv[tri_loops]
        d1 = tri[:, 1] - tri[:, 0]
        d2 = tri[:, 2] - tri[:, 0]
        area = 0.5 * np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]).sum()
        total += area
        shapes.append((loops, pts, tri, area))
    if total <= 0:
        return None

    def rot(p, k):
        for _ in range(k % 4):
            p = np.stack([-p[..., 1], p[..., 0]], -1)
        return p

    def extent(i):
        e = shapes[i][1].max(0) - shapes[i][1].min(0)
        return e

    # Plusieurs ordres de placement sont essayés ; on garde le plus dense.
    orders = [sorted(range(len(shapes)), key=lambda i: -shapes[i][3])]
    if len(shapes) <= 150:
        orders.append(sorted(range(len(shapes)), key=lambda i: -extent(i).max()))
        orders.append(sorted(range(len(shapes)), key=lambda i: -(extent(i).max() * extent(i).min()) ** 0.5 - shapes[i][3]))
    rotations = (0, 1, 2, 3) if len(shapes) <= 150 else (0, 1)

    def attempt(scale, order):
        occ = np.zeros((G, G), dtype=np.float32)
        out = uv.copy()
        for i in order:
            loops, pts, tri, _ = shapes[i]
            best = None
            occ_f = np.fft.rfft2(occ)
            for k in rotations:
                pr = rot(pts, k)
                mn = pr.min(0)
                ext = (pr.max(0) - mn) * scale
                w = int(math.ceil(ext[0])) + 2 * r + 1
                h = int(math.ceil(ext[1])) + 2 * r + 1
                if w > G or h > G:
                    continue
                tp = (rot(tri, k) - mn) * scale + r
                m = dilate(conservative_mask(tp, h, w), r)
                mp = np.zeros((G, G), dtype=np.float32)
                mp[:h, :w] = m
                corr = np.fft.irfft2(occ_f * np.conj(np.fft.rfft2(mp)), s=(G, G))
                free = corr[: G - h + 1, : G - w + 1] < 0.5
                if not free.any():
                    continue
                ys, xs = np.nonzero(free)
                score = np.maximum(ys + h, xs + w) * (2 * G) + (ys + h) + (xs + w)
                j = int(np.argmin(score))
                cand = (score[j], k, int(xs[j]), int(ys[j]), m, mn)
                if best is None or cand[0] < best[0]:
                    best = cand
            if best is None:
                return None
            _, k, x, y, m, mn = best
            occ[y : y + m.shape[0], x : x + m.shape[1]] += m
            out[loops] = ((rot(pts, k) - mn) * scale + r + np.array([x, y])) / G
        return out

    winner, winner_scale = None, 0.0
    for order in orders:
        hi = G * math.sqrt(1.0 / total)
        lo = max(G * math.sqrt(fill_lo / total), winner_scale)
        best = attempt(lo, order)
        while best is None and lo > 1e-9:
            hi = lo
            lo *= 0.7
            best = attempt(lo, order)
        if best is None:
            continue
        for _ in range(8):
            mid = math.sqrt(lo * hi)
            res = attempt(mid, order)
            if res is None:
                hi = mid
            else:
                lo, best = mid, res
            if hi / lo < 1.01:
                break
        if lo > winner_scale:
            winner, winner_scale = best, lo
    return winner


def erode(mask, r):
    out = mask.copy()
    pad = np.pad(mask, r, constant_values=False)
    h, w = mask.shape
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out &= pad[r + dy : r + dy + h, r + dx : r + dx + w]
    return out


def dilate(mask, r):
    out = mask.copy()
    pad = np.pad(mask, r, constant_values=False)
    h, w = mask.shape
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= pad[r + dy : r + dy + h, r + dx : r + dx + w]
    return out


def raster_count(tri_px, res):
    """Compte, par pixel, les triangles qui couvrent son centre.
    `tri_px` : (T, 3, 2) en coordonnées pixel."""
    cover = np.zeros((res, res), dtype=np.int32)
    for p in tri_px:
        x0, y0 = np.maximum(np.floor(p.min(0)).astype(int), 0)
        x1, y1 = np.minimum(np.ceil(p.max(0)).astype(int), res)
        if x1 <= x0 or y1 <= y0:
            continue
        X, Y = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        (ax, ay), (bx, by), (cx, cy) = p
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-12:
            continue
        l1 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) / d
        l2 = ((cy - ay) * (X - cx) + (ax - cx) * (Y - cy)) / d
        inside = (l1 > 1e-4) & (l2 > 1e-4) & (1 - l1 - l2 > 1e-4)
        cover[y0:y1, x0:x1] += inside
    return cover


def min_area_rect_angle(pts):
    """Angle de rotation qui minimise l'aire de la boîte englobante (calipers)."""
    hull = convex_hull(pts)
    if len(hull) < 3:
        return 0.0
    best, best_angle = math.inf, 0.0
    for i in range(len(hull)):
        e = hull[(i + 1) % len(hull)] - hull[i]
        ang = math.atan2(e[1], e[0])
        cs, sn = math.cos(-ang), math.sin(-ang)
        r = hull @ np.array([[cs, -sn], [sn, cs]]).T
        ext = r.max(0) - r.min(0)
        a = ext[0] * ext[1]
        if a < best - 1e-15:
            best, best_angle = a, ang
    return best_angle


def convex_hull(pts):
    p = np.unique(np.round(pts, 12), axis=0)
    if len(p) < 3:
        return p
    p = p[np.lexsort((p[:, 1], p[:, 0]))]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for q in p:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    for q in p[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return np.array(lower[:-1] + upper[:-1])


# ---------------------------------------------------------------------------
# Images de contrôle et transfert de texture
# ---------------------------------------------------------------------------


def save_layout_image(path, cover, owner, n_islands):
    res = cover.shape[0]
    rng = np.random.default_rng(1)
    palette = rng.uniform(0.35, 0.95, size=(max(n_islands, 1), 3))
    img = np.ones((res, res, 4), dtype=np.float32) * np.array([0.08, 0.08, 0.1, 1.0], dtype=np.float32)
    m = owner >= 0
    img[m, :3] = palette[owner[m]]
    img[cover > 1, :3] = (1.0, 0.0, 0.0)
    write_png(path, img)


def write_png(path, rgba):
    h, w, _ = rgba.shape
    name = "__uvopt_tmp__"
    if name in bpy.data.images:
        bpy.data.images.remove(bpy.data.images[name])
    im = bpy.data.images.new(name, width=w, height=h, alpha=True)
    im.pixels.foreach_set(np.ascontiguousarray(rgba, dtype=np.float32).ravel())
    im.filepath_raw = os.path.abspath(path)
    im.file_format = "PNG"
    im.save()
    bpy.data.images.remove(im)


def find_base_color_image(obj):
    for slot in obj.material_slots:
        mat = slot.material
        if not mat or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                sock = node.inputs.get("Base Color")
                if sock and sock.is_linked:
                    src = sock.links[0].from_node
                    if src.type == "TEX_IMAGE" and src.image:
                        return src.image
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image:
                return node.image
    return None


def setup_cycles(scene, samples):
    scene.render.engine = "CYCLES"
    scene.cycles.samples = samples
    scene.cycles.use_denoising = False
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for backend in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
            try:
                prefs.compute_device_type = backend
                prefs.get_devices()
                if any(d.type == backend for d in prefs.devices):
                    for d in prefs.devices:
                        d.use = d.type == backend
                    scene.cycles.device = "GPU"
                    return backend
            except TypeError:
                continue
    except Exception:
        pass
    scene.cycles.device = "CPU"
    return "CPU"


def transfer_texture(obj, args):
    """Reprojette l'ancienne texture couleur (UV d'origine) sur les nouveaux UV."""
    me = obj.data
    if ORIGINAL_UV_NAME not in me.uv_layers:
        return None
    src = find_base_color_image(obj)
    if src is None:
        return None
    size = args.texture_size
    out = bpy.data.images.new("BaseColor_transfer", width=size, height=size, alpha=False)
    out.colorspace_settings.name = "sRGB"
    mat = bpy.data.materials.new("M_UVOptimized")
    if bpy.app.version < (5, 0, 0):  # toujours actif à partir de Blender 5
        mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    uvsrc = nt.nodes.new("ShaderNodeUVMap")
    uvsrc.uv_map = ORIGINAL_UV_NAME
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = src
    emit = nt.nodes.new("ShaderNodeEmission")
    outn = nt.nodes.new("ShaderNodeOutputMaterial")
    target = nt.nodes.new("ShaderNodeTexImage")
    target.image = out
    nt.links.new(uvsrc.outputs["UV"], tex.inputs["Vector"])
    nt.links.new(tex.outputs["Color"], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], outn.inputs["Surface"])
    nt.nodes.active = target
    me.materials.clear()
    me.materials.append(mat)
    for p in me.polygons:
        p.material_index = 0
    scene = bpy.context.scene
    device = setup_cycles(scene, 1)
    scene.render.bake.margin = args.padding
    scene.render.bake.margin_type = "EXTEND"
    scene.render.bake.use_clear = True
    me.uv_layers.active = me.uv_layers[NEW_UV_NAME]
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    t = time.time()
    bpy.ops.object.bake(type="EMIT", margin=args.padding, use_clear=True)
    log(f"Texture transférée sur les nouveaux UV ({device}, {time.time() - t:.1f} s)")
    path = os.path.splitext(args.output)[0] + "_basecolor_transfer.png"
    out.filepath_raw = os.path.abspath(path)
    out.file_format = "PNG"
    out.save()

    # Matériau final propre : couleur transférée -> Principled BSDF.
    nt.nodes.clear()
    tex2 = nt.nodes.new("ShaderNodeTexImage")
    tex2.image = out
    uvn = nt.nodes.new("ShaderNodeUVMap")
    uvn.uv_map = NEW_UV_NAME
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    outn = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(uvn.outputs["UV"], tex2.inputs["Vector"])
    nt.links.new(tex2.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(bsdf.outputs["BSDF"], outn.inputs["Surface"])
    return path


def render_checker_preview(obj, path, args):
    """Rendu de contrôle : damier sur les nouveaux UV, 4 vues assemblées."""
    scene = bpy.context.scene
    me = obj.data
    mat = bpy.data.materials.new("M_UVChecker")
    if bpy.app.version < (5, 0, 0):  # toujours actif à partir de Blender 5
        mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    uvn = nt.nodes.new("ShaderNodeUVMap")
    uvn.uv_map = NEW_UV_NAME
    chk = nt.nodes.new("ShaderNodeTexChecker")
    chk.inputs["Scale"].default_value = 48.0
    chk.inputs["Color1"].default_value = (0.9, 0.9, 0.9, 1)
    chk.inputs["Color2"].default_value = (0.15, 0.15, 0.18, 1)
    grid = nt.nodes.new("ShaderNodeTexChecker")
    grid.inputs["Scale"].default_value = 6.0
    grid.inputs["Color1"].default_value = (1.0, 0.45, 0.2, 1)
    grid.inputs["Color2"].default_value = (0.25, 0.55, 1.0, 1)
    mix = nt.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.blend_type = "MULTIPLY"
    mix.inputs["Factor"].default_value = 0.6
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Roughness"].default_value = 0.6
    outn = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(uvn.outputs["UV"], chk.inputs["Vector"])
    nt.links.new(uvn.outputs["UV"], grid.inputs["Vector"])
    nt.links.new(chk.outputs["Color"], mix.inputs["A"])
    nt.links.new(grid.outputs["Color"], mix.inputs["B"])
    nt.links.new(mix.outputs["Result"], bsdf.inputs["Base Color"])
    nt.links.new(bsdf.outputs["BSDF"], outn.inputs["Surface"])
    saved = list(me.materials)
    saved_idx = [p.material_index for p in me.polygons]
    me.materials.clear()
    me.materials.append(mat)

    setup_cycles(scene, 24)
    scene.cycles.use_denoising = False
    scene.render.resolution_x = scene.render.resolution_y = 640
    scene.render.film_transparent = False
    world = bpy.data.worlds.new("W") if scene.world is None else scene.world
    scene.world = world
    if bpy.app.version < (5, 0, 0):
        world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.6, 0.6, 0.62, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 1.0
    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("Sun", sun_data)
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)

    co = np.array([v.co[:] for v in me.vertices])
    center = (co.max(0) + co.min(0)) / 2
    radius = float(np.linalg.norm(co.max(0) - co.min(0))) / 2 or 1.0
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = radius * 2.1
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    tiles = []
    tmp = os.path.splitext(path)[0] + "_tmp.png"
    for az in (-90, 0, 90, 180):
        a = math.radians(az)
        el = math.radians(15)
        pos = Vector(center) + Vector((math.cos(el) * math.cos(a), math.cos(el) * math.sin(a), math.sin(el))) * radius * 4
        cam.location = pos
        cam.rotation_euler = (Vector(center) - pos).to_track_quat("-Z", "Y").to_euler()
        scene.render.filepath = tmp
        bpy.ops.render.render(write_still=True)
        im = bpy.data.images.load(tmp)
        px = np.array(im.pixels[:], dtype=np.float32).reshape(im.size[1], im.size[0], 4)
        tiles.append(px)
        bpy.data.images.remove(im)
    os.remove(tmp)
    top = np.concatenate([tiles[2], tiles[3]], axis=1)
    bottom = np.concatenate([tiles[0], tiles[1]], axis=1)
    write_png(path, np.concatenate([bottom, top], axis=0))

    me.materials.clear()
    for m in saved:
        me.materials.append(m)
    for p, i in zip(me.polygons, saved_idx):
        p.material_index = i
    bpy.data.objects.remove(cam)
    bpy.data.objects.remove(sun)
    log(f"Rendu damier : {path}")


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------


def run(args):
    t0 = time.time()
    log(f"Import : {args.input}")
    obj = import_mesh(args.input)
    original_edges = preprocess(obj, args)
    opt = UVOptimizer(obj, args)
    opt.segment()
    if not args.no_merge:
        opt.merge_small()
    opt.smooth_boundaries()
    opt.ensure_disks()
    opt.unwrap()
    opt.detach_thin_parts()
    opt.split_elongated()
    # Revalidation : le lissage et le détachement ont remodelé des îlots.
    opt.segment()
    if not args.no_straighten:
        opt.straighten()
    opt.layout()

    # Réparation : un îlot qui se chevauche lui-même est redécoupé (contrôle
    # fin) ; en dernier recours, les faces fautives sont détachées.
    opt.shape_res = 768
    for attempt in range(4):
        lab, n = opt.labels()
        _, _, pairs = opt.rasterize(lab, 1024)
        selfo = sorted({p[0] for p in pairs if p[0] == p[1]})
        if not selfo:
            break
        log(f"Réparation de {len(selfo)} îlot(s) qui se chevauchent")
        if attempt < 2:
            charts = opt.charts(lab, n)
            for c in selfo:
                opt.split_chart(charts[c], evaluate=True)
        else:
            opt.detach_faces({int(opt.topo.tri_face[t]) for t in opt.overlap_tris})
        opt.ensure_disks()
        opt.unwrap()
        opt.layout()

    rep, cover, owner = opt.report(time.time() - t0)
    bpy.ops.object.mode_set(mode="OBJECT")
    if original_edges is not None and not args.keep_quads:
        n = restore_triangulation(obj, original_edges)
        log(f"Triangulation d'origine restaurée ({n} quads recoupés)")
    save_layout_image(args.layout, cover, owner, rep["islands"])

    if not args.no_transfer:
        path = transfer_texture(obj, args)
        rep["basecolor_transfer"] = os.path.abspath(path) if path else None
    if args.sym:
        mirror_half(obj, args)
        rep["symmetry"] = {
            "mode": "mirror",
            "plane_x": round(args.sym["plane_x"], 5),
            "error_mean_percent": round(100 * args.sym["error_mean"], 4),
            "note": "Mesures UV pour une moitié ; l'autre moitié partage les mêmes UV (miroir).",
        }
        log("Moitié miroir recréée (UV superposées)")
    else:
        rep["symmetry"] = {"mode": "off"}
    if args.preview:
        render_checker_preview(obj, args.preview, args)
        rep["preview"] = os.path.abspath(args.preview)
    if args.save_blend:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args.save_blend))
    if not args.keep_original_uv and ORIGINAL_UV_NAME in obj.data.uv_layers:
        obj.data.uv_layers.remove(obj.data.uv_layers[ORIGINAL_UV_NAME])
    export_mesh(obj, args)
    rep["seconds"] = round(time.time() - t0, 1)
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2, ensure_ascii=False)

    log("—" * 60)
    log(f"Statut : {rep['status']}")
    log(f"Îlots : {rep['islands']}   Remplissage UV : {rep['uv_coverage_percent']} %")
    log(f"Distorsion moyenne : {rep['distortion_mean']}   "
        f"Surface déformée > 10 % : {rep['distortion_area_over_10pct_percent']} %")
    td = rep["texel_density_px_per_m"]
    log(f"Densité de texels : {td['median']} px/m (min {td['min']}, max {td['max']})")
    log(f"Chevauchements : {rep['overlap_pixels_percent']} %   Triangles retournés : {rep['flipped_triangles']}")
    for w in rep["warnings"]:
        log(f"ATTENTION : {w}")
    log(f"Export : {args.output}   Rapport : {args.report}   Disposition : {args.layout}")
    return rep


def main(argv=None):
    args = parse_args(sys.argv if argv is None else argv)
    return run(args)


if __name__ == "__main__":
    main()
