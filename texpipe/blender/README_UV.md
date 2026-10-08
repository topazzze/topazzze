# Optimisation UV (`uv_optimize.py`)

Dépliage UV automatique de qualité production pour les meshes low poly de jeu
(pensé pour les « Smart Low Poly » de Tripo3D : robots, décors industriels,
pierre). Le script tourne dans Blender, sans interface.

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
8. **Densité de texels uniforme**, réduite de moitié sur les zones cachées
   (`--hidden-density 0.5`), orientation optimale de chaque îlot, puis
   rangement serré avec une marge en pixels adaptée aux mipmaps
   (`--padding`, par défaut taille / 256 : 16 px en 4K).

## Réglages utiles

| Option | Défaut | Effet |
|---|---|---|
| `--quality` | `balanced` | `seams` = moins de coutures, `distortion` = étirement minimal |
| `--texture-size` | 4096 | Sert à la marge en pixels et au calcul de densité |
| `--sharp-angle` | 65 | Baisser (ex. 45) pour plus d'arêtes vives sur du très anguleux |
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
