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
   - Priorité à l'image de face : elle décide partout où elle voit
     correctement la surface ; le dos puis les côtés complètent le reste. Des
     images pas tout à fait cohérentes entre elles ne se mélangent donc pas.
   - Les gris du dessin (contours, ombrages, reflets) sont rattachés soit au
     noir du corps, soit au gris des rotules, selon leur clarté.
   - Les parties vues par aucune image (creux, dessous) reçoivent le matériau
     principal (`--hidden-fill neighbors` pour prolonger les zones voisines).
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

## Étape 6 bis : matériaux photo (AmbientCG, Poly Haven)

Variante de l'étape 6 avec de vraies textures photo, CC0 (usage commercial
libre, sans attribution).

**1. Recherche et images des candidats**

```bat
texpipe\find_materials.bat robot_uv.glb
```

- Pour chaque zone, des matériaux correspondant à son type sont cherchés sur
  les deux sites. Par exemple, `metal_sombre` donne « black metal »,
  « dark metal » et « painted metal ».
- Ils sont téléchargés en 1K dans `F:\Pipeline\library` (réutilisée d'un asset
  à l'autre) et classés selon leur ressemblance avec la couleur de la zone.
- Les 4 meilleurs sont appliqués tour à tour à la zone, et l'objet est rendu
  dans Blender. Les petites zones (rotules) ont en plus un gros plan, face à la
  zone.
- Planches produites :
  - `robot_zoneN_candidats.png` : une par zone ;
  - `robot_materiaux_candidats.png` : toutes les zones.

  Sous chaque rendu : le nom du matériau, un échantillon de sa texture, et un
  cadre vert sur le choix actuel.

Options :
- `--count 6` : plus de candidats.
- `--query "1=carbon fiber"` : recherche personnalisée pour une zone. On peut
  aussi l'écrire dans `zones.txt` : `3 = metal_nu #8c8c8c recherche=grey_steel`.

Pour savoir quels mots utiliser (« métal gris », « tôle rouillée »…), voir le
[glossaire des matériaux](GLOSSAIRE_MATERIAUX.md).

**2. Validation**

Ouvrir `robot_materials_choice.txt`, qui contient une ligne par zone :

```
1 = ambientcg:Metal049A
2 = emissif
3 = polyhaven:metal_plate taille=0.3
```

- On peut y mettre n'importe quel candidat des planches, ou `procedural`
  pour garder le matériau calculé.
- `taille=` règle la taille du motif, en mètres.

**3. Scène Blender**

```bat
texpipe\open_materials.bat robot_uv.glb
```

Télécharge les matériaux retenus en 2K, construit `robot_materials.blend`,
puis l'ouvre dans Blender.

**Un objet par zone.** Dans la collection `robot`, chaque zone est un objet
(`robot_Z1_metal_sombre`, `robot_Z2_emissif`…). Tous partagent le même mesh,
et chacun a son propre matériau :

- Chaque matériau est visible seulement sur sa zone : un masque, au pixel près,
  pilote la transparence. Les LED gardent donc leur précision, même quand
  elles traversent les faces du mesh.
- Sélectionner un objet donne accès au matériau de sa zone seule.
- Masquer un objet dans l'outliner montre sa zone en creux.
- Les objets sont décalés de 0,05 mm (modificateur « Decalage_zone ») pour
  éviter les conflits d'affichage entre surfaces superposées.
- `--single-material` : un seul objet avec un seul matériau qui mélange les
  zones, comme avant.

Chaque matériau contient :

- un groupe de nœuds pour le matériau de la zone ;
- textures plaquées en projection boîte (taille réelle, sans couture UV) ;
- relief du HighPoly (normal map de l'étape 3) avec le micro-relief des
  textures ;
- usure des arêtes (courbure) et saleté des creux (occlusion) ;
- LED en émission.

Options :
- `--wear`, `--dirt` : réglage de l'usure et de la saleté.
- `--emission` : intensité des LED.
- `--pack` : textures embarquées dans le `.blend`.
