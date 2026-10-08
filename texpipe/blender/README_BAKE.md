# Calcul des cartes (`bake_maps.py`)

Étape 3 du pipeline. Le mesh détaillé (HighPoly Tripo) est projeté sur le
mesh de jeu déplié à l'étape 2. Ses détails deviennent des textures sur les
UV du mesh de jeu. Le calcul se fait dans Blender (Cycles), sur la carte
graphique si elle est disponible.

## Utilisation

```bat
texpipe\bake_maps.bat robot_uv.glb robot_high.glb --texture-size 4096 --preview apercu_bake.png
```

Le mesh de jeu doit être la sortie de l'étape 2 (`*_uv.glb`). Le HighPoly
doit venir de la même génération Tripo, pour que les deux se superposent ; le
script prévient sinon.

## Fichiers produits

Dans le dossier du mesh de jeu (`--output-dir` pour en changer), avec le nom
du mesh sans `_uv` :

| Fichier | Contenu | Servira à |
|---|---|---|
| `_normal.png` | Normal map tangente, 16 bits, convention DirectX (Unreal) | Le relief du HighPoly sur le mesh de jeu |
| `_ao.png` | Occlusion ambiante | Saleté et ombres dans les creux |
| `_curvature.png` | Courbure : 0,5 = plat, clair = arête vive, sombre = creux | Usure des arêtes, poussière dans les creux |
| `_height.png` | Hauteur dans l'objet (0 = bas, 1 = haut), 16 bits | Saleté qui remonte du sol |
| `_up.png` | Orientation vers le haut (1 = face au ciel) | Poussière, neige, mousse sur le dessus |
| `_basecolor_high.png` | Couleur Tripo du HighPoly | Guide pour le découpage en zones (étape 5) |
| `_roughness_high.png`, `_metallic_high.png` | Rugosité et métal Tripo (s'ils existent) | Idem |
| `_bake_check.png` | En rouge : pixels où le HighPoly n'a pas été trouvé | Contrôle |
| `_bake_report.json` | Mesures et contrôles | Contrôle |
| `--preview` | Rendus : mesh de jeu avec ses cartes, puis HighPoly, de face et de 3/4 | Contrôle visuel |

## Ce que fait le script

1. **Symétrie (automatique)** : les parties du mesh qui partagent exactement
   les mêmes UV (moitié recréée en miroir à l'étape 2, ou îlots empilés) ne
   sont calculées qu'une fois. Les copies sont sorties de la texture le temps
   du calcul et réutilisent ensuite les pixels de l'original. Le reflet est
   correct dans Unreal grâce à MikkTSpace. Sur un mesh non symétrique, il n'y
   a rien à mettre de côté : tout est calculé. `--bake-side -X` choisit
   l'autre côté, `--mirror off` désactive la détection.
2. **Distance de projection automatique** : l'écart entre les deux surfaces
   est mesuré dans les deux sens. Une passe rapide compte ensuite les pixels
   qui ne trouvent pas le HighPoly. La distance n'est allongée que si cela les
   réduit nettement, car une projection trop longue attrape d'autres surfaces.
3. **Zones sans correspondance** : le LowPoly « intelligent » de Tripo
   simplifie certaines formes, par exemple une face qui comble l'espace entre
   deux plumes. Derrière ces faces, il n'y a pas de HighPoly. Là, la normal
   map garde la forme du mesh de jeu, avec un fondu, et les autres cartes sont
   complétées par les pixels voisins. Sur l'Archange, cela touche environ 3 %
   des pixels.
4. **Normal map** depuis la géométrie seule. La normal map Tripo du HighPoly,
   souvent bruitée, est ignorée sauf avec `--high-normal-map`.
5. **Courbure** calculée à partir de la normal map, sur 4 échelles (arêtes
   fines et formes plus larges).
6. **Débord** autour des îlots (`--margin`, défaut taille / 128 : 32 px en 4K),
   pour des mipmaps propres.

## Réglages utiles

| Option | Défaut | Effet |
|---|---|---|
| `--texture-size` | 4096 | Résolution des cartes |
| `--normal-format` | `directx` | `opengl` pour Blender ou Unity |
| `--cage` | auto | Distance de projection en mètres |
| `--ao-distance` | auto | Portée de l'occlusion (5 % de la taille de l'objet) |
| `--ao-samples` | 128 | Qualité de l'occlusion (moins de grain si plus haut) |
| `--maps` | toutes | Par exemple `--maps normal,ao` |
| `--cpu` | — | Forcer le calcul sur le processeur |

## Mesures de référence

Archange (4 786 triangles, HighPoly de 2 millions de triangles), texture
1024, sur un processeur de serveur à 4 cœurs : 85 s pour toutes les cartes et
l'aperçu. En 4K sur une RTX 3070 Ti, compter quelques minutes, l'occlusion
étant la carte la plus longue.

## Tests

```bash
pytest tests/test_bake_maps.py
```
