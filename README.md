# topazzze — pipeline de texturing pour meshes Tripo3D

Objectif : à partir d'un mesh Tripo3D (version optimisée + version détaillée)
et d'une image de référence, produire des matériaux PBR réalistes pour
Unreal Engine, avec des outils gratuits utilisables commercialement.

## Briques disponibles

| Étape | Script | État |
|---|---|---|
| Dépliage UV de qualité production | [`texpipe/blender/uv_optimize.py`](texpipe/blender/README_UV.md) | ✅ testé |
| Calcul des cartes (normales, AO, courbure) | — | à venir |
| Couleur guide (MV-Adapter / StableGen) | — | à venir |
| Découpage en zones de matériau | — | à venir |
| Composition des couches de matériau | — | à venir |
| Export et import Unreal | — | à venir |

Installation (Windows) : double-clic sur `install.bat` (Git, Blender, Python 3.11,
PyTorch CUDA, bibliothèques, MV-Adapter). Ajouter `-Models` pour télécharger
aussi les modèles IA (~8 Go) : `install.bat -Models`.

Démarrage rapide (Windows) :

```bat
texpipe\uv_optimize.bat robot_low.glb robot_low_uv.glb --texture-size 4096 --preview apercu.png
```
