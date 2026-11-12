# Données hors ligne de Moto Alerte

Données publiques préparées chaque mois pour l'application Android **Moto Alerte**
(Île-de-France + Oise) : aucune donnée personnelle, aucun code de l'application.

| Fichier | Contenu |
|---|---|
| `roads.bin` | Tronçons roulables d'OpenStreetMap : limitations (dans chaque sens), noms, numéros, panneaux des bretelles, graphe d'itinéraire, interdictions de tourner, sorties d'autoroute, index spatial |
| `search.sqlite` | Communes, voies et numéros (Base Adresse Nationale, à défaut BANO) et lieux utiles d'OpenStreetMap, index plein texte |
| `map.pmtiles` | Fond de carte vectoriel (schéma OpenMapTiles, généré avec planetiler) |

L'appli lit `https://github.com/promeneur95/moto-alerte-data/releases/download/data/manifest.json`
(version, adresses, tailles et empreintes SHA-256 des fichiers de la dernière version).

Préparation : `.github/workflows/build-data.yml` (le 3 de chaque mois, ou à la demande).

## Licences

- © contributeurs OpenStreetMap — données sous licence ODbL (https://www.openstreetmap.org/copyright).
  `roads.bin`, `map.pmtiles` et la partie « lieux » de `search.sqlite` sont des bases dérivées ODbL.
- Base Adresse Nationale — Licence Ouverte / Open Licence (Etalab) ; BANO — ODbL.
- Fond de carte : schéma OpenMapTiles (CC-BY 4.0), © OpenMapTiles.
