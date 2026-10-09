# Glossaire des matériaux (AmbientCG, Poly Haven)

Ce glossaire sert à décrire ce que l'on veut (« métal gris », « tôle
rouillée »…) avec les mots qui trouvent les bons matériaux sur
[AmbientCG](https://ambientcg.com/) et [Poly Haven](https://polyhaven.com/textures).
Les deux sites sont en anglais, sous licence CC0 (usage commercial libre,
sans attribution).

---

## 1. Réponse rapide : « métal gris »

Dans `robot_zones.txt`, mets le type `metal_nu`, une couleur grise et, si tu
veux préciser, des mots de recherche (espaces remplacés par `_`) :

```
3 = metal_nu  #8c8c8c  recherche=grey_steel
```

| Tu veux… | Type (zones.txt) | Couleur | `recherche=` |
|---|---|---|---|
| Métal gris clair, neuf | `metal_nu` | `#b4b4b4` | `steel` ou `aluminium` |
| Métal gris moyen (acier) | `metal_nu` | `#8c8c8c` | `grey_steel` |
| Métal gris foncé (canon de fusil) | `metal_sombre` | `#4a4a4c` | `dark_metal` ou `gunmetal` |
| Métal gris brossé | `metal_brosse` | `#a0a0a0` | `brushed_metal` |
| Métal gris peint (mat) | `metal_peint` | `#6e6e6e` | `grey_painted_metal` |
| Métal gris usé, rayé | `metal_nu` | `#8c8c8c` | `scratched_metal` |
| Métal gris galvanisé | `metal_nu` | `#9a9c9e` | `galvanized_steel` |

La recherche est lancée par `texpipe\find_materials.bat robot_uv.glb`. Sans
`recherche=`, des mots par défaut sont utilisés selon le type (section 2).
On peut aussi passer les mots dans la commande :
`--query "3=grey steel"`.

---

## 2. Les types du pipeline (`zones.txt`)

| Type | Pour | Mots cherchés par défaut | Métal ? |
|---|---|---|---|
| `metal_sombre` | Métal noir ou très foncé, armures | black metal, dark metal, painted metal | indifférent |
| `metal_peint` | Métal recouvert de peinture | painted metal, metal paint | indifférent |
| `metal_nu` | Métal gris sans peinture (acier, fer) | metal, steel, iron | oui |
| `metal_brosse` | Métal brossé (stries fines) | brushed metal | oui |
| `chrome` | Métal poli miroir | chrome, polished metal | oui |
| `or` | Or, doré | gold | oui |
| `cuivre` | Cuivre | copper | oui |
| `plastique` | Plastique | plastic | non |
| `caoutchouc` | Caoutchouc, joints, pneus | rubber | non |
| `pierre` | Roche, pierre taillée | rock, stone | non |
| `beton` | Béton, ciment | concrete | non |
| `emissif` | Lumières, LED (aucune recherche) | — | — |

« Métal ? » : les candidats dont la carte de métal ne correspond pas sont
classés plus loin.

---

## 3. Glossaire français → mots de recherche

Les mots de recherche sont en anglais. La colonne « Type » indique quoi
mettre dans `zones.txt`.

### Métaux

| Français | Mots de recherche | Type |
|---|---|---|
| Acier | steel | `metal_nu` |
| Acier inoxydable (inox) | stainless steel | `metal_nu` / `chrome` |
| Aluminium | aluminium, aluminum | `metal_nu` |
| Fer | iron | `metal_nu` |
| Fonte | cast iron | `metal_sombre` |
| Métal brossé | brushed metal | `metal_brosse` |
| Métal poli, miroir | polished metal, chrome | `chrome` |
| Métal rayé, usé | scratched metal, worn metal | `metal_nu` |
| Métal martelé | hammered metal | `metal_nu` |
| Métal galvanisé | galvanized steel | `metal_nu` |
| Métal anodisé | anodized metal | `metal_peint` |
| Métal peint | painted metal | `metal_peint` |
| Peinture écaillée | chipped paint metal, peeling paint | `metal_peint` |
| Métal rouillé | rust, rusty metal | `metal_peint` |
| Tôle | sheet metal | `metal_nu` |
| Tôle ondulée | corrugated steel, corrugated metal | `metal_nu` |
| Tôle larmée (antidérapante) | diamond plate | `metal_nu` |
| Plaques rivetées | metal plates, riveted metal | `metal_nu` |
| Caillebotis, grille de sol | metal walkway, metal grate | `metal_nu` |
| Grillage | chainlink fence | `metal_nu` |
| Cotte de mailles | chainmail | `metal_nu` |
| Cuivre | copper | `cuivre` |
| Laiton | brass | `or` |
| Bronze | bronze | `cuivre` |
| Or | gold | `or` |
| Titane | titanium | `metal_nu` |

### Plastiques, caoutchouc, composites

| Français | Mots de recherche | Type |
|---|---|---|
| Plastique lisse | plastic | `plastique` |
| Plastique texturé | textured plastic | `plastique` |
| Caoutchouc | rubber | `caoutchouc` |
| Fibre de carbone | carbon fiber | `plastique` |
| Mousse | foam | `plastique` |

### Pierre, béton, construction

| Français | Mots de recherche | Type |
|---|---|---|
| Roche brute | rock | `pierre` |
| Pierre taillée | stone, stone blocks | `pierre` |
| Marbre | marble | `pierre` |
| Granit | granite | `pierre` |
| Béton | concrete | `beton` |
| Béton coffré, brut | raw concrete, concrete wall | `beton` |
| Enduit, plâtre | plaster | `beton` |
| Brique | bricks | `pierre` |
| Carrelage | tiles | `pierre` |
| Pavés | paving stones | `pierre` |
| Asphalte, goudron | asphalt | `beton` |

### Autres

| Français | Mots de recherche | Type |
|---|---|---|
| Bois | wood | `plastique` (non métal) |
| Planches | planks, wood floor | `plastique` |
| Cuir | leather | `caoutchouc` |
| Tissu | fabric | `caoutchouc` |
| Terre, sol | ground, soil | `pierre` |
| Gravier | gravel | `pierre` |

Pour un matériau qui n'est dans aucun type (bois, cuir…), choisis le type le
plus proche pour le métal (`metal_*`) ou non (`plastique`, `caoutchouc`), et
précise avec `recherche=`.

---

## 4. Mots qui précisent l'aspect

Ils s'ajoutent à n'importe quel mot ci-dessus : `recherche=dark_scratched_steel`.

| Français | Anglais |
|---|---|
| Gris / foncé / clair | grey (ou gray) / dark / light |
| Noir / blanc | black / white |
| Neuf, propre | clean, new |
| Usé | worn |
| Rayé | scratched |
| Sale | dirty |
| Rouillé | rusty |
| Écaillé | chipped, peeling |
| Brillant / mat | glossy, polished / matte |
| Brossé | brushed |
| Industriel | industrial |

---

## 5. Couleurs de gris (pour `zones.txt`)

| Nom | Code | Usage typique |
|---|---|---|
| Aluminium | `#c0c0c0` | métal clair, neuf |
| Acier clair | `#a8a8a8` | métal brossé |
| Acier | `#8c8c8c` | métal gris standard |
| Gris moyen | `#6e6e6e` | métal peint gris |
| Gris canon (gunmetal) | `#4a4a4c` | métal foncé bleuté |
| Anthracite | `#2e2e30` | métal très foncé |
| Noir | `#1a1a1a` | métal noir, armure |

La couleur sert à classer les candidats : le matériau dont la texture
ressemble le plus à cette couleur arrive en premier.

---

## 6. Organisation des deux sites

**Poly Haven** : 12 catégories de textures.

| Catégorie | Contenu |
|---|---|
| Metal | métaux, plaques, rouille |
| Stone | roche, pierre, marbre |
| Concrete | béton, enduits |
| Brick & Block | briques, parpaings |
| Ceramic | carrelage, céramique |
| Asphalt & Bitumen | routes, goudron |
| Ground & Terrain | sols, terre, sable |
| Organic | écorces, végétaux |
| Plastic & Rubber | plastiques, caoutchouc |
| Paper & Card | papier, carton |
| Textiles & Leather | tissus, cuirs |
| Wood | bois, planches |

**AmbientCG** : les matériaux sont rangés en séries numérotées. Par exemple :
- `Metal009` ;
- `MetalPlates006` ;
- `DiamondPlate006` ;
- `PaintedMetal004`.

Chaque série est décrite par des mots-clés : pour `Metal009`, *Brushed,
Bumpy, Metal, Scratches, Silver, Steel*. La recherche du pipeline
interroge ces mots-clés, d'où l'intérêt des mots de la section 3.

Pour retenir directement un matériau vu sur un des sites, mets son
identifiant dans `robot_materials_choice.txt` :

```
1 = ambientcg:Metal009
3 = polyhaven:<identifiant>
```

L'identifiant est le nom dans l'adresse de la page : `ambientcg.com/view?id=Metal009`,
`polyhaven.com/a/<identifiant>`.

---

Sources :
- [Poly Haven, catégories de textures](https://polyhaven.com/textures)
- [AmbientCG](https://ambientcg.com/), exemples
  [Metal 009](https://ambientcg.com/view?id=Metal009) et
  [Metal 038](https://ambientcg.com/view?id=Metal038)
- [Matériaux AmbientCG (Metal Plates, Diamond Plate, Metal Factory Floor)](https://sbox.game/sausages/ambientcgmat)
