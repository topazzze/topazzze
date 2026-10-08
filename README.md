# topazzze — pipeline de texturing pour meshes Tripo3D

Objectif : à partir d'un mesh Tripo3D (version optimisée + version détaillée)
et d'une image de référence, produire des matériaux PBR réalistes pour
Unreal Engine, avec des outils gratuits utilisables commercialement.

## Briques disponibles

| Étape | Script | État |
|---|---|---|
| Dépliage UV de qualité production | [`texpipe/blender/uv_optimize.py`](texpipe/blender/README_UV.md) | ✅ testé |
| Calcul des cartes (normales, AO, courbure) | [`texpipe/blender/bake_maps.py`](texpipe/blender/README_BAKE.md) | ✅ testé |
| Couleur guide (MV-Adapter) | [`texpipe/ai/color_guide.py`](texpipe/ai/README_COLOR.md) | ✅ géométrie testée, IA à valider sur GPU |
| Découpage en zones de matériau | [`texpipe/ai/zones.py`](texpipe/ai/README_MATERIALS.md) | ✅ testé |
| Composition des couches de matériau | [`texpipe/ai/materials.py`](texpipe/ai/README_MATERIALS.md) | ✅ testé |
| Export et import Unreal | à la main (voir WORKFLOW.txt) | — |

Installation (Windows) : double-clic sur `install.bat` (détecte Blender, installe
Python 3.11 si besoin, PyTorch CUDA, bibliothèques, MV-Adapter ; ni winget ni Git requis). Ajouter `-Models` pour télécharger
aussi les modèles IA (~6 Go) : `install.bat -Models`.

Démarrage rapide (Windows) :

```bat
texpipe\uv_optimize.bat robot_low.glb robot_low_uv.glb --texture-size 4096 --preview apercu.png
texpipe\bake_maps.bat robot_low_uv.glb robot_high.glb --texture-size 4096 --preview apercu_bake.png
texpipe\color_guide.bat robot_low_uv.glb robot_front.png
texpipe\zones.bat robot_low_uv.glb
texpipe\materials.bat robot_low_uv.glb
```
