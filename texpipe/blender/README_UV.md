# Optimisation UV (`uv_optimize.py`)

Dépliage UV automatique de qualité production pour les meshes low poly de jeu
(pensé pour les « Smart Low Poly » de Tripo3D : robots, décors industriels,
pierre). Le script tourne dans Blender, sans interface.

## Conseil Tripo : exporter le LowPoly en quads

Un LowPoly exporté **en quads** depuis Tripo donne des UV bien plus propres
qu'en triangles (sur l'Archange : 59 à 65 îlots au lieu de 110, bords plus
nets). Le glTF stocke toujours des triangles, mais le script retrouve les
quads d'origine d'après l'ordre du fichier, puis restaure la triangulation
exacte à l'export.

## Utilisation

Windows (adapter le chemin de Blender, 4.2 LTS ou plus récent) :

```bat
"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe" -b --factory-startup ^
    -P texpipe\blender\uv_optimize.py -- ^
    --input  robot_low.glb ^
    --output robot_low_uv.glb ^
    --texture-size 4096 ^
    --preview robot_low_uv_preview.png
```

Ou avec le raccourci fourni (édite la ligne `BLENDER` une fois) :

```bat
texpipe\uv_optimize.bat robot_low.glb robot_low_uv.glb --texture-size 4096
```

Formats acceptés : `.glb`, `.gltf`, `.fbx`, `.obj`, `.blend` en entrée ;
`.glb`, `.fbx`, `.obj`, `.blend` en sortie.

## Fichiers produits

| Fichier | Contenu |
|---|---|
| `robot_low_uv.glb` | Le mesh avec ses nouveaux UV, triangulation d'origine conservée |
| `robot_low_uv_uv_report.json` | Mesures de qualité (voir plus bas) et statut `OK` / `ATTENTION` |
| `robot_low_uv_uv_layout.png` | Disposition des îlots dans la texture (rouge = chevauchement) |
| `robot_low_uv_basecolor_transfer.png` | L'ancienne texture Tripo reprojetée sur les nouveaux UV |
| `--preview` | Rendu damier sous 4 angles : les carrés doivent être réguliers partout |

## Ce que fait le script

1. **Nettoyage** : fusion des objets, soudure des sommets dupliqués par le
   glTF, normales recalculées. Les triangles sont regroupés en quads
   *temporaires* (pour pouvoir redresser les îlots), puis la triangulation
   d'origine est restaurée à l'identique à la fin : le bake de normal map
   verra la même triangulation que le moteur.
2. **Arêtes vives** (au-delà de `--sharp-angle`, 65° par défaut) : chacune
   devient une couture, règle indispensable pour un bake propre. Les arêtes
   vives isolées ou qui découperaient des micro-îlots (bruit de génération
   IA) sont adoucies : le relief passera par la normal map.
3. **Coût des coutures** : chaque arête reçoit un coût selon sa visibilité
   (occlusion ambiante, orientation : le dos et le dessous sont « cachés »)
   et sa forme (les creux et les plis cachent bien une couture).
4. **Topologie** : chaque îlot doit pouvoir s'aplatir. Les tubes (bras,
   pistons, câbles) reçoivent **une seule couture droite** du côté le moins
   visible, les formes fermées (rotules) sont coupées en deux, les anses sont
   ouvertes par un graphe de coupe minimal.
5. **Distorsion, compacité, replis** : dépliage *Minimum Stretch* (SLIM),
   puis mesure de l'étirement de chaque triangle. Un îlot trop déformé, trop
   étalé (bras fins qui gaspillent la texture) ou replié sur lui-même est
   redécoupé. Plusieurs découpes sont essayées et notées (coût de la
   couture, équilibre, distorsion réelle après un dépliage d'essai).
6. **Fusion** : les îlots voisins sont recollés quand le résultat reste peu
   déformé et compact (moins de coutures, moins de fragments).
7. **Redressement** : les îlots en grille de quads deviennent des rectangles
   parfaits (textures répétées alignées, rangement plus dense).
8. **Lissage des frontières et détachement des parties fines** : les bords en
   dents de scie sont lissés, les bandes étroites accrochées aux îlots
   (bandeaux de chanfrein, « moustaches ») sont détachées puis redressées.
9. **Densité de texels uniforme**, réduite de moitié sur les zones cachées
   (`--hidden-density 0.5`), orientation optimale de chaque îlot.
10. **Îlots trop longs coupés en travers** : un îlot plus long que la racine
   de la surface totale limiterait l'échelle de tout le rangement (il doit
   tenir dans la largeur de la texture). Il est coupé au milieu, sur les
   arêtes les moins visibles. Sur l'Archange : remplissage 56 % -> 68 %.
11. **Rangement par forme réelle** (principe de xatlas) : chaque îlot est
   rastérisé et placé, sous 4 orientations, dans le premier creux libre, y compris
   dans les concavités des autres îlots. Marge en pixels adaptée aux mipmaps
   (`--padding`, par défaut taille / 256 : 16 px en 4K). Bien plus dense que
   le rangement intégré de Blender (voir les mesures plus bas).
12. **Contrôle final** : chevauchements recherchés pixel par pixel, îlots
   fautifs redécoupés automatiquement.

## Symétrie (miroir)

Les meshes Tripo générés de face sont presque toujours symétriques. Le script
le détecte (`--symmetry auto`, par défaut) : il coupe le mesh en deux, déplie
**une seule moitié** sur toute la texture, puis recrée l'autre moitié en
miroir avec **les mêmes UV**. Résultat : environ deux fois plus de pixels par
surface, et une jonction invisible au milieu (la texture s'y reflète). C'est
la pratique standard des productions de jeu.

- Le mesh exporté est complet ; seule sa moitié gauche est un reflet exact de
  la droite (écart mesuré et affiché, 0,004 % sur l'Archange).
- `--symmetry off` : chaque côté a ses propres UV, pour des détails
  asymétriques (usure, marquages différents à gauche et à droite).
- Le calcul des cartes (normal map, AO) devra se faire sur une seule moitié,
  puisque les deux côtés partagent les mêmes pixels : l'étape suivante du
  pipeline en tiendra compte.

## Réglages utiles

| Option | Défaut | Effet |
|---|---|---|
| `--symmetry` | `auto` | `off` pour des détails différents à gauche et à droite |
| `--quality` | `balanced` | `seams` = moins de coutures, `distortion` = étirement minimal |
| `--texture-size` | 4096 | Sert à la marge en pixels et au calcul de densité |
| `--sharp-angle` | auto | 65°, relevé seul sur les meshes très facettés (Tripo : ~115°) ; ou une valeur fixe |
| `--hidden-density` | 0.5 | 1.0 = même résolution partout |
| `--front` | `-Y` | Face avant du mesh (convention glTF : -Y dans Blender) |
| `--min-compactness` | 0.45 | Monter pour un rangement plus serré (plus d'îlots) |
| `--save-blend` | — | Garde une scène .blend avec les anciens et nouveaux UV |

## Lire le rapport

- `uv_coverage_percent` : part de la texture réellement utilisée. Au-dessus
  de 65 % c'est bon, au-dessus de 75 % c'est excellent.
- `distortion_mean` : étirement moyen (0.05 = 5 %). Sous 0.08 c'est propre.
- `distortion_area_over_10pct_percent` : part de la surface étirée de plus
  de 10 %.
- `texel_density_px_per_m` : pixels par mètre. `uniformity_percent` à 100 %
  signifie une densité identique partout (hors réduction voulue des zones
  cachées).
- `hard_edges_not_seam` doit valoir 0, `flipped_triangles` aussi.
- `overlap_pixels_percent` : chevauchements, doit être (quasi) nul.

## Mesures de référence

Mesurées sur un CPU de serveur (sans carte graphique ; seul le transfert de
texture utilise Cycles, sur GPU s'il y en a un), texture 2048 :

| Mesh | Faces | Îlots | Remplissage | Distorsion moy. | Chevauchements | Durée |
|---|---|---|---|---|---|---|
| Robot de test (cylindres, sphères, boîtes chanfreinées) | 1 354 tris | 59 | 86 % | 0,013 | 0 | ~1 min |
| DamagedHelmet (Khronos, mesh de jeu triangulé) | 15 452 tris | 189 | 67 % | 0,062 | 0 | ~8 min |

À titre de comparaison, le rangement intégré de Blender plafonnait à 47-49 %
de remplissage sur le casque, quels que soient ses réglages.

## Tests

```bash
pip install bpy numpy pytest     # bpy = Blender comme module Python
pytest tests/
```
