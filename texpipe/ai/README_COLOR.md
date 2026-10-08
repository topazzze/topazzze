# Couleur guide par IA (`color_guide.py`)

Étape 4 du pipeline. MV-Adapter génère 6 vues de l'objet (face, droite, dos,
gauche, dessus, dessous). La génération est guidée par :

- l'image de référence de face ;
- la forme exacte du mesh de jeu (images de position et de normales).

Si les images **dos, gauche et droite** sont rangées à côté de l'image de face
avec le même nom (`Robot_Front.jpeg`, `Robot_Back.jpeg`, `Robot_Left.jpeg`,
`Robot_Right.jpeg`), elles sont reconnues automatiquement. Chacune est recalée
sur la silhouette du mesh ; si elle s'y superpose bien (80 % ou plus), elle
remplace la vue inventée par l'IA. Sur l'Archange, la superposition est de
91 à 93 %.

Les vues sont ensuite reprojetées sur les UV. Le résultat est une texture
couleur alignée sur le mesh. Elle guidera le découpage en zones (étape 5) et
le choix des matériaux (étape 6) ; ce n'est pas la texture finale.

## Utilisation

```bat
texpipe\color_guide.bat robot_uv.glb robot_front.png
```

Le premier lancement télécharge les modèles (~6 Go, dans `models\` du
pipeline). La génération prend environ 1 à 2 minutes sur une RTX 3070 Ti.

| Fichier | Contenu |
|---|---|
| `_views.png` | Les 6 vues générées : à vérifier en premier |
| `_views_used.png` | Les vues réellement reprojetées, avec tes images dos et côtés recalées |
| `_color_guide.png` | Les vues reprojetées sur les UV (2048 px) |
| `_reference.png` | La référence telle que donnée à l'IA (fond retiré, recadrée) |
| `_control.png` | Les vues de guidage (position, normales), pour contrôle |
| `_color_report.json` | Mesures, dont la part de surface vue par au moins une vue |

## Réglages utiles

| Option | Défaut | Effet |
|---|---|---|
| `--seed` | 42 | Changer pour obtenir une autre proposition |
| `--prompt` | — | Description en anglais, ex. `"dark metal robot, purple glowing lines"` |
| `--reference-scale` | 1.0 | Fidélité à l'image de référence |
| `--variant` | `sd21` | `sdxl` : plus fin (768 px), mais lent sur 8 Go (déchargement partiel) |
| `--views` | — | Reprojeter des vues existantes (retouchées à la main) sans relancer l'IA |
| `--texture-size` | 2048 | Résolution de la texture guide |
| `--references` | `auto` | `off` : n'utiliser que les vues de l'IA |
| `--min-overlap` | 0.8 | Superposition minimale pour utiliser une image dos / côté |
| `--bg-tolerance` | 12 | Détourage du fond uni des références |

## Détails techniques

- **Pas de nvdiffrast**, dont la licence NVIDIA est non commerciale. Les vues
  de guidage et la reprojection sont calculées en numpy, avec exactement les
  caméras de MV-Adapter. Un test vérifie l'aller-retour vue -> texture
  (erreur médiane < 1 %).
- **Reprojection** : chaque pixel de texture prend la moyenne des vues qui
  le voient, pondérée par l'angle. La visibilité est testée en profondeur et
  les bords de silhouette sont exclus. Les parties jamais vues (dessous des
  bras, creux) sont complétées par les pixels voisins.
- **Symétrie** : si l'étape 2 a mis les deux côtés en miroir, la couleur du
  côté +X est utilisée pour les deux.
- **Modèle de base** : SD 2.1 a été retiré du compte officiel de Stability
  sur Hugging Face. Le script essaie ensuite une copie des mêmes poids
  (format safetensors uniquement) ; `--base-model` permet d'en indiquer une
  autre.

## Licences à vérifier avant un usage commercial

- Code MV-Adapter : Apache-2.0.
- Poids MV-Adapter (`huanngzh/mv-adapter`) : licence non confirmée ; voir
  leur page Hugging Face.
- Stable Diffusion 2.1 et SDXL : CreativeML Open RAIL++-M (usage commercial
  autorisé, avec des restrictions d'usage).
