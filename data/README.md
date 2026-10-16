# Données

Jeux de données géographiques ouverts utilisés par la plateforme.

## `togo-regions.geojson`

Limites des **5 régions administratives** du Togo (du nord au sud) : **Savanes,
Kara, Centrale, Plateaux, Maritime**. Chargé à la **tâche 6.2** et amorcé dans la
table de référence `region` par `app/seed/regions.py`.

Chaque *feature* porte des propriétés stables permettant l'amorçage et le rendu
de la carte :

| propriété          | exemple                 | rôle                                   |
|--------------------|-------------------------|----------------------------------------|
| `code`             | `MARITIME`              | code canonique stable (clé d'upsert)   |
| `name_fr`          | `Maritime`              | nom français affiché                   |
| `shapeISO`         | `TG-M`                  | code ISO 3166-2:TG de provenance       |
| `source_shapeName` | `Maritime Region`       | nom d'origine du jeu de données        |
| `source_shapeID`   | `44829518B32493797...`  | identifiant d'origine (traçabilité)    |

Le **Grand Lomé** n'est **pas** une 6ᵉ région : il est modélisé comme une
sous-zone de la région **Maritime**. Le fichier contient donc exactement les 5
régions canoniques (`admUnitCount = 5` à la source).

### Provenance et attribution

Les limites proviennent de **geoBoundaries** (jeu de données ouvert), release
`gbOpen`, Togo ADM1 (régions), identifiant `TGO-ADM1-44829518`,
année représentée 2017.

- **Source des données :** contributeurs d'OpenStreetMap, via geoBoundaries
  (frontières OSM de Wambacher).
- **Licence :** Open Data Commons Open Database License (**ODbL**) 1.0 —
  <https://opendatacommons.org/licenses/odbl/1-0/>
- **© OpenStreetMap :** <https://www.openstreetmap.org/copyright>
- **Fournisseur :** geoBoundaries — <https://www.geoboundaries.org>
- **Citation :** Runfola, D. et al. (2020) *geoBoundaries: A global database of
  political administrative boundaries.* PLoS ONE 15(4): e0231866.
- **URL de téléchargement (épinglée) :**
  <https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/TGO/ADM1/geoBoundaries-TGO-ADM1.geojson>

> Note sur la licence : la revue des exigences mentionnait un jeu CC-BY-4.0. La
> source ADM1 faisant autorité et réellement disponible pour le Togo
> (geoBoundaries / OpenStreetMap) est publiée sous **ODbL 1.0**, également une
> licence ouverte attribuée. L'attribution ci-dessus (et le bloc `attribution`
> en tête du GeoJSON) satisfait l'exigence « jeu de données ouvert et attribué »
> (REQ-7.6). L'attribution ODbL doit rester visible partout où la carte est
> rendue.

L'attribution est aussi dupliquée dans une propriété de haut niveau
`attribution` du `FeatureCollection` (un fichier GeoJSON ne peut pas contenir de
commentaires), afin qu'elle voyage avec les données.
