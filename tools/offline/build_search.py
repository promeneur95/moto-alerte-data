#!/usr/bin/env python3
"""
Base de recherche hors ligne de Moto Alerte (search.sqlite).

  - communes et voies de la Base Adresse Nationale (BAN), avec tous les numéros de chaque voie ;
  - lieux utiles d'OpenStreetMap (gares, stations-service, mairies, hôpitaux, commerces…).

Table `places` (id, kind 0 commune / 1 voie / 2 lieu, name, detail, lat, lon, nums) et index plein
texte FTS4 `places_fts(norm)` (docid = id) sur un texte normalisé : minuscules, sans accents ni
ponctuation — l'appli normalise la saisie de la même façon (SearchIndex.normalize).
`nums` : pour une voie, suite de [longueur u8][numéro ASCII][lat×1e6 i32][lon×1e6 i32].

    python3 build_search.py search.sqlite --ban adresses-95.csv.gz ... --pois pois.osm.pbf [--bbox w,s,e,n]
"""
import argparse
import csv
import gzip
import io
import math
import sqlite3
import struct
import sys
import unicodedata

import osmium

CATEGORIES = {
    ("amenity", "fuel"): "Station-service", ("amenity", "charging_station"): "Borne de recharge",
    ("amenity", "parking"): "Parking", ("amenity", "motorcycle_parking"): "Parking moto",
    ("amenity", "restaurant"): "Restaurant", ("amenity", "fast_food"): "Restauration rapide",
    ("amenity", "cafe"): "Café", ("amenity", "bar"): "Bar", ("amenity", "pub"): "Bar",
    ("amenity", "hospital"): "Hôpital", ("amenity", "clinic"): "Clinique", ("amenity", "pharmacy"): "Pharmacie",
    ("amenity", "police"): "Police", ("amenity", "fire_station"): "Pompiers", ("amenity", "townhall"): "Mairie",
    ("amenity", "post_office"): "Bureau de poste", ("amenity", "school"): "École", ("amenity", "college"): "Lycée",
    ("amenity", "university"): "Université", ("amenity", "cinema"): "Cinéma", ("amenity", "theatre"): "Théâtre",
    ("amenity", "library"): "Bibliothèque", ("amenity", "bank"): "Banque", ("amenity", "marketplace"): "Marché",
    ("amenity", "place_of_worship"): "Lieu de culte",
    ("shop", "supermarket"): "Supermarché", ("shop", "mall"): "Centre commercial", ("shop", "motorcycle"): "Moto",
    ("shop", "car_repair"): "Garage", ("shop", "car"): "Concession auto", ("shop", "bakery"): "Boulangerie",
    ("shop", "doityourself"): "Bricolage", ("shop", "department_store"): "Grand magasin",
    ("tourism", "hotel"): "Hôtel", ("tourism", "museum"): "Musée", ("tourism", "attraction"): "Site touristique",
    ("tourism", "viewpoint"): "Point de vue", ("tourism", "camp_site"): "Camping",
    ("railway", "station"): "Gare", ("railway", "halt"): "Gare",
    ("leisure", "park"): "Parc", ("leisure", "stadium"): "Stade", ("leisure", "sports_centre"): "Centre sportif",
    ("leisure", "golf_course"): "Golf", ("aeroway", "aerodrome"): "Aérodrome",
    ("historic", "castle"): "Château", ("historic", "monument"): "Monument",
}


DIGITS = "0123456789"


def normalize(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    out = []
    for ch in s:
        out.append(ch if ch.isalnum() else " ")
    return " ".join("".join(out).replace("œ", "oe").replace("æ", "ae").split())


class Pois(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.items = []
        self.bbox = bbox

    def _add(self, tags, lat, lon):
        name = tags.get("name")
        if not name:
            return
        cat = None
        for k in ("amenity", "shop", "tourism", "railway", "leisure", "aeroway", "historic"):
            v = tags.get(k)
            if v and (k, v) in CATEGORIES:
                cat = CATEGORIES[(k, v)]
                break
        if cat is None:
            return
        if self.bbox and not (self.bbox[1] <= lat <= self.bbox[3] and self.bbox[0] <= lon <= self.bbox[2]):
            return
        self.items.append((name, cat, lat, lon))

    def node(self, n):
        if n.tags and n.location.valid():
            self._add(n.tags, n.location.lat, n.location.lon)

    def way(self, w):
        if not w.tags or not w.tags.get("name"):
            return
        la = lo = 0.0
        k = 0
        for nr in w.nodes:
            if nr.location.valid():
                la += nr.location.lat; lo += nr.location.lon; k += 1
        if k:
            self._add(w.tags, la / k, lo / k)


def _rows(path):
    """Lignes normalisées d'un fichier BAN (« ; », avec en-tête) ou BANO (« , », sans en-tête)."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        head = f.readline()
        f.seek(0)
        if "nom_voie" in head:
            for row in csv.DictReader(f, delimiter=";"):
                yield (row["code_insee"], row["nom_commune"], row["code_postal"], row["nom_voie"],
                       (row.get("numero") or "").strip(), (row.get("rep") or "").strip(), row["lat"], row["lon"])
        else:
            # BANO : id, numero, voie, cp, ville, source, lat, lon
            for r in csv.reader(f):
                if len(r) < 8:
                    continue
                num = r[1].strip()
                m = 0
                while m < len(num) and num[m] in DIGITS:
                    m += 1
                yield (r[0][:5], r[4], r[3], r[2], num[:m], num[m:].strip().lower(), r[6], r[7])


def read_ban(paths, bbox):
    voies = {}
    communes = {}
    for p in paths:
        for insee, commune, cp, nom, num, rep, la, lo in _rows(p):
            try:
                lat = float(la); lon = float(lo)
            except ValueError:
                continue
            if bbox and not (bbox[1] <= lat <= bbox[3] and bbox[0] <= lon <= bbox[2]):
                continue
            key = (insee, nom)
            v = voies.get(key)
            if v is None:
                v = voies[key] = {"nom": nom, "commune": commune, "cp": cp, "nums": []}
            if num and num != "99999":  # 99999 : lieu-dit sans numéro
                v["nums"].append((num + rep, lat, lon))
            else:
                v.setdefault("ld", (lat, lon))
            c = communes.setdefault(insee, {"nom": commune, "cp": cp, "lat": 0.0, "lon": 0.0, "n": 0})
            c["lat"] += lat; c["lon"] += lon; c["n"] += 1
    return voies, communes


def build(out, ban, pois_pbf, bbox):
    voies, communes = read_ban(ban, bbox)
    print(f"BAN : {len(communes)} communes, {len(voies)} voies", file=sys.stderr)
    db = sqlite3.connect(out)
    db.executescript("""
        PRAGMA page_size = 4096;
        DROP TABLE IF EXISTS places; DROP TABLE IF EXISTS places_fts; DROP TABLE IF EXISTS meta;
        CREATE TABLE places (id INTEGER PRIMARY KEY, kind INTEGER NOT NULL, name TEXT NOT NULL, detail TEXT,
                             lat REAL NOT NULL, lon REAL NOT NULL, nums BLOB);
        CREATE VIRTUAL TABLE places_fts USING fts4(norm, tokenize=simple);
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    rows = []
    fts = []
    pid = 0
    centres = []
    for insee, c in communes.items():
        lat, lon = c["lat"] / c["n"], c["lon"] / c["n"]
        pid += 1
        rows.append((pid, 0, c["nom"], c["cp"], lat, lon, None))
        fts.append((pid, normalize(f'{c["nom"]} {c["cp"]}')))
        centres.append((lat, lon, c["nom"]))
    for (insee, nom), v in voies.items():
        nums = v["nums"]
        if nums:
            nums.sort(key=lambda x: (int("".join(ch for ch in x[0] if ch in DIGITS) or 0), x[0]))
            lat = sum(n[1] for n in nums) / len(nums); lon = sum(n[2] for n in nums) / len(nums)
            blob = bytearray()
            for num, la, lo in nums:
                b = num.encode("ascii", "ignore")[:20]
                blob += struct.pack("<B", len(b)) + b + struct.pack("<ii", int(round(la * 1e6)), int(round(lo * 1e6)))
            blob = bytes(blob)
        else:
            lat, lon = v.get("ld", (None, None))
            if lat is None:
                continue
            blob = None
        pid += 1
        rows.append((pid, 1, nom, f'{v["cp"]} {v["commune"]}', lat, lon, blob))
        fts.append((pid, normalize(f'{nom} {v["commune"]} {v["cp"]}')))

    if pois_pbf:
        h = Pois(bbox)
        h.apply_file(pois_pbf, locations=True, idx="flex_mem")
        print(f"OSM : {len(h.items)} lieux", file=sys.stderr)
        # Commune la plus proche (centre des adresses), pour l'affichage et la recherche « gare cergy »
        cell = {}
        for la, lo, nm in centres:
            cell.setdefault((int(la * 20), int(lo * 20)), []).append((la, lo, nm))

        def nearest(la, lo):
            best = None; bd = 1e18
            r0, c0 = int(la * 20), int(lo * 20)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    for a, b, nm in cell.get((r0 + dr, c0 + dc), ()):
                        d = (a - la) ** 2 + ((b - lo) * math.cos(math.radians(la))) ** 2
                        if d < bd:
                            bd = d; best = nm
            return best

        seen = set()
        for name, cat, la, lo in h.items:
            key = (name, cat, round(la, 3), round(lo, 3))
            if key in seen:
                continue
            seen.add(key)
            com = nearest(la, lo)
            pid += 1
            rows.append((pid, 2, name, f"{cat} · {com}" if com else cat, la, lo, None))
            fts.append((pid, normalize(f"{name} {cat} {com or ''}")))

    db.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO places_fts(docid, norm) VALUES (?,?)", fts)
    db.execute("INSERT INTO places_fts(places_fts) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"search.sqlite : {len(rows)} entrées", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--ban", nargs="*", default=[])
    ap.add_argument("--pois")
    ap.add_argument("--bbox", help="ouest,sud,est,nord")
    a = ap.parse_args()
    bb = [float(x) for x in a.bbox.split(",")] if a.bbox else None
    build(a.out, a.ban, a.pois, bb)
