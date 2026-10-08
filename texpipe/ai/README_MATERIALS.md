# Zones et matériaux (`zones.py`, `materials.py`)

## Étape 5 : zones

```bat
texpipe\zones.bat robot_uv.glb
```

Découpe la surface en zones de matériau, propose un matériau par zone et
écrit `robot_zones.txt`, à vérifier et corriger. L'aperçu
`robot_zones_preview.png` montre les zones numérotées sous 4 côtés.

**Source des couleurs, de la plus fiable à la moins fiable :**

1. **Images unlit** (aplats de couleur sans éclairage) nommées
   `robot_Front_Unlit.png`, et `_Back_Unlit`, `_Left_Unlit`, `_Right_Unlit`
   si possible. Elles sont recalées sur la silhouette du mesh, leurs couleurs
   sont classées sur l'image même, puis les zones sont reprojetées sur les UV.
   - Les bords anticrénelés (mélange de deux aplats) sont rattachés à
     l'aplat le plus proche.
   - Les LED gardent des bords nets.
   - Les côtés non vus sont complétés : lignes lumineuses reprises des images
     éclairées, le reste étendu depuis les zones voisines.
2. **Sans image unlit** : couleur guide de l'étape 4 et couleur Tripo. Les
   reflets et halos de ces images éclairées rendent les limites moins
   précises.

Format de `robot_zones.txt` : une ligne par zone, `numéro = matériau #couleur`.

```
1 = metal_sombre #282828
2 = emissif      #a836f9
3 = metal_nu     #696868
```

## Étape 6 : matériaux

```bat
texpipe\materials.bat robot_uv.glb --wear 0.5 --dirt 0.5 --dust 0.2
```

Chaque zone reçoit un matériau PBR procédural. Aucune texture n'est
téléchargée, donc aucune question de licence. Les motifs viennent d'un bruit
3D évalué sur la surface : pas de couture aux bords des îlots UV.

| Matériau | Métal | Rugosité | À l'usure |
|---|---|---|---|
| metal_peint, plastique | non | 0,42 / 0,50 | la peinture s'écaille (métal nu) / s'éclaircit |
| metal_nu, metal_sombre, metal_brosse | oui | 0,30 à 0,38 | se polit |
| chrome, or, cuivre | oui | 0,07 à 0,30 | se polit |
| caoutchouc, pierre, beton | non | 0,85 à 0,90 | s'éclaircit |
| emissif | non | 0,30 | pas d'usure ; la couleur de la zone devient l'émission |

**Couches d'usure** (réglables de 0 à 1) :
- `--wear` : arêtes usées, d'après la courbure de l'étape 3.
- `--dirt` : saleté dans les creux et en bas, d'après l'occlusion, la
  courbure et la hauteur.
- `--dust` : poussière sur les surfaces tournées vers le haut.

La normal map finale combine le relief du HighPoly et le micro-relief du
matériau (méthode RNM), en convention DirectX.

**Sorties :** `T_robot_BC.png`, `T_robot_N.png`, `T_robot_ORM.png`,
`T_robot_E.png`, et l'aperçu `robot_apercu_materiaux.png` (rendu Cycles).
