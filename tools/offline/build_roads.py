#!/usr/bin/env python3
"""
Base routière hors ligne de Moto Alerte (roads.bin), à partir d'un extrait OpenStreetMap (.osm.pbf).

Contenu, lu tel quel par l'appli (fichier projeté en mémoire, petit-boutiste, entiers 32 bits) :
  - tronçons roulables (enregistrement de 32 octets, dont l'indice de leur premier arc) : type de voie, sens unique, limitations (générale / sens du tracé / sens
    inverse), nom, numéro, panneaux de direction des bretelles, géométrie ;
  - graphe d'itinéraire : nœuds de jonction, arcs (longueur, durée dans chaque sens), adjacence ;
  - interdictions de tourner (no_* / only_*, via un nœud) ;
  - numéros de sortie d'autoroute (motorway_junction) ;
  - index spatial en grille (≈ 1,1 km) des tronçons.

La lecture des tags reproduit exactement OsmTags.kt (parseMaxspeed, maxspeedOf, parseOneway).

    python3 build_roads.py extrait.osm.pbf roads.bin [--version 20261112]
"""
import argparse
import math
import re
import struct
import sys
from array import array

import osmium

MAGIC = b"MAR1"
FORMAT = 1

HIGHWAYS = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
            "living_street", "service", "road", "motorway_link", "trunk_link", "primary_link",
            "secondary_link", "tertiary_link"]
HW_CODE = {h: i for i, h in enumerate(HIGHWAYS)}
SERVICE_EXCLUDED = {"parking_aisle", "driveway", "drive-through", "emergency_access"}
NO = {"no", "private"}
YES = {"yes", "designated", "permissive", "destination", "customers", "delivery"}

# Vitesse pratiquée ≈ limite × facteur (modèle de durée de l'itinéraire)
FACTOR = {"motorway": 0.92, "trunk": 0.88, "primary": 0.8, "secondary": 0.76, "tertiary": 0.72,
          "unclassified": 0.66, "residential": 0.6, "living_street": 0.5, "service": 0.45, "road": 0.55,
          "motorway_link": 0.75, "trunk_link": 0.72, "primary_link": 0.7, "secondary_link": 0.7,
          "tertiary_link": 0.7}

CELL_LAT = 0.01
CELL_LON = 0.015


# ----------------------------------------------------------------- tags (miroir d'OsmTags.kt)

_num = re.compile(r"(\d{1,3})(?:\s*(mph|km/h|kmh))?\s*$")


def parse_maxspeed(raw):
    if raw is None or not raw.strip():
        return None
    v = raw.strip().lower()
    fixed = {"none": None, "signals": None, "variable": None, "walk": 10, "fr:walk": 20,
             "fr:living_street": 20, "fr:urban": 50, "fr:rural": 80, "fr:trunk": 110, "fr:motorway": 130}
    if v in fixed:
        return fixed[v]
    m = _num.search(v)
    if not m:
        return None
    n = int(m.group(1))
    if n == 0:
        return None
    return int(n * 1.609) if m.group(2) == "mph" else n


def maxspeed_of(tags):
    for k in ("maxspeed", "zone:maxspeed", "maxspeed:type", "source:maxspeed"):
        v = parse_maxspeed(tags.get(k))
        if v is not None:
            return v
    return None


def parse_oneway(oneway, highway, junction):
    o = (oneway or "").strip().lower()
    if o in ("yes", "true", "1"):
        return 1
    if o in ("-1", "reverse"):
        return -1
    if o in ("no", "false", "0", "alternating", "reversible"):
        return 0
    return 1 if highway in ("motorway", "motorway_link") or junction in ("roundabout", "circular") else 0


def default_for(highway):
    return {"motorway": 130, "trunk": 110, "motorway_link": 90, "trunk_link": 90, "primary_link": 70,
            "secondary_link": 70, "tertiary_link": 70, "primary": 80, "secondary": 80, "tertiary": 80,
            "unclassified": 80, "residential": 50, "service": 30, "living_street": 20, "road": 50}.get(highway)


def drivable(tags):
    hw = tags.get("highway")
    if hw not in HW_CODE:
        return False
    if tags.get("area") == "yes":
        return False
    if hw == "service" and tags.get("service") in SERVICE_EXCLUDED:
        return False
    for k in ("motorcycle", "motor_vehicle", "vehicle", "access"):
        v = tags.get(k)
        if v in YES:
            return True
        if v in NO:
            return False
    return True


def destinations(tags):
    """Format OSRM : « A 15: Cergy-Pontoise, Franconville »."""
    d = tags.get("destination")
    r = tags.get("destination:ref")
    places = ", ".join(p.strip() for p in d.split(";") if p.strip()) if d else ""
    refs = ", ".join(p.strip() for p in r.split(";") if p.strip()) if r else ""
    if refs and places:
        return f"{refs}: {places}"
    return places or (f"{refs}:" if refs else None)


def haversine(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# ----------------------------------------------------------------- lecture OSM

class Reader(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.ways = []          # (osm_id, tags dict réduit, nodes array('q'), lats array('i'), lons array('i'))
        self.exits = {}         # osm node → numéro de sortie
        self.restrictions = []  # (from_way, via_node, to_way, kind)

    def node(self, n):
        if n.tags.get("highway") == "motorway_junction":
            ref = n.tags.get("ref")
            if ref:
                self.exits[n.id] = ref

    def way(self, w):
        t = w.tags
        if not drivable(t):
            return
        nodes = array("q")
        lats = array("i")
        lons = array("i")
        for nr in w.nodes:
            if not nr.location.valid():
                continue  # nœud hors de l'extrait
            nodes.append(nr.ref)
            lats.append(int(round(nr.location.lat * 1e7)))
            lons.append(int(round(nr.location.lon * 1e7)))
        if len(nodes) < 2:
            return
        keep = {k: t.get(k) for k in ("highway", "name", "ref", "oneway", "junction", "maxspeed", "zone:maxspeed",
                                       "maxspeed:type", "source:maxspeed", "maxspeed:forward", "maxspeed:backward",
                                       "destination", "destination:ref", "tunnel", "access") if t.get(k) is not None}
        self.ways.append((w.id, keep, nodes, lats, lons))

    def relation(self, r):
        t = r.tags
        if t.get("type") not in ("restriction", "restriction:motorcar", "restriction:motorcycle"):
            return
        kind_raw = t.get("restriction") or t.get("restriction:motorcar") or t.get("restriction:motorcycle")
        if not kind_raw:
            return
        exc = t.get("except") or ""
        if "motorcycle" in exc or "motorcar" in exc:
            return
        if kind_raw.startswith("no_"):
            kind = 0
        elif kind_raw.startswith("only_"):
            kind = 1
        else:
            return
        frm = via = to = None
        for m in r.members:
            if m.role == "from" and m.type == "w":
                frm = m.ref
            elif m.role == "to" and m.type == "w":
                to = m.ref
            elif m.role == "via":
                if m.type != "n":
                    return  # interdiction « via un tronçon » : non gérée
                via = m.ref
        if frm and via and to:
            self.restrictions.append((frm, via, to, kind))


# ----------------------------------------------------------------- écriture

class Strings:
    def __init__(self):
        self.blob = bytearray()
        self.index = {}

    def ref(self, s):
        if not s:
            return -1
        s = s.strip()
        if not s:
            return -1
        if s in self.index:
            return self.index[s]
        b = s.encode("utf-8")[:65535]
        off = len(self.blob)
        self.blob += struct.pack("<H", len(b)) + b
        self.index[s] = off
        return off


def build(pbf, out, version):
    rd = Reader()
    rd.apply_file(pbf, locations=True, idx="flex_mem")
    ways = rd.ways
    print(f"{len(ways)} tronçons roulables, {len(rd.restrictions)} interdictions, {len(rd.exits)} sorties", file=sys.stderr)

    # Nœuds de jonction : extrémités et nœuds partagés (ou répétés dans un même tronçon)
    use = {}
    for _, _, nodes, _, _ in ways:
        for nd in nodes:
            use[nd] = use.get(nd, 0) + 1
    gid = {}
    for _, _, nodes, _, _ in ways:
        for i, nd in enumerate(nodes):
            if (i == 0 or i == len(nodes) - 1 or use[nd] > 1) and nd not in gid:
                gid[nd] = len(gid)
    del use
    n_g = len(gid)

    strings = Strings()
    way_rec = bytearray()
    pt_lat = array("i"); pt_lon = array("i"); pt_g = array("i")
    g_pt = array("i", [0]) * n_g
    segs = bytearray()
    seg_from = array("i"); seg_to = array("i")
    way_index = {}
    n_seg = 0

    # Grille
    all_lat_min = min(min(w[3]) for w in ways); all_lat_max = max(max(w[3]) for w in ways)
    all_lon_min = min(min(w[4]) for w in ways); all_lon_max = max(max(w[4]) for w in ways)
    cell_lat = int(CELL_LAT * 1e7); cell_lon = int(CELL_LON * 1e7)
    g_min_lat = all_lat_min - cell_lat // 2; g_min_lon = all_lon_min - cell_lon // 2
    rows = (all_lat_max - g_min_lat) // cell_lat + 1
    cols = (all_lon_max - g_min_lon) // cell_lon + 1
    cells = {}

    for wi, (osm_id, t, nodes, lats, lons) in enumerate(ways):
        way_index[osm_id] = wi
        hw = t["highway"]
        junction = t.get("junction")
        oneway = parse_oneway(t.get("oneway"), hw, junction)
        ms = maxspeed_of(t)
        mf = parse_maxspeed(t.get("maxspeed:forward"))
        mb = parse_maxspeed(t.get("maxspeed:backward"))
        flags = 0
        if junction in ("roundabout", "circular"):
            flags |= 1
        if t.get("tunnel") in ("yes", "building_passage"):
            flags |= 2
        if t.get("access") == "destination":
            flags |= 4
        pt_start = len(pt_lat)
        way_rec += struct.pack("<iiBbBBBBHiiii", pt_start, len(nodes), HW_CODE[hw], oneway, flags,
                               min(ms or 0, 255), min(mf or 0, 255), min(mb or 0, 255), 0,
                               strings.ref(t.get("name")), strings.ref(t.get("ref")),
                               strings.ref(destinations(t)), n_seg)
        for i, nd in enumerate(nodes):
            pt_lat.append(lats[i]); pt_lon.append(lons[i])
            g = gid.get(nd, -1)
            pt_g.append(g)
            if g >= 0:
                g_pt[g] = pt_start + i
        # Index spatial : cellules couvertes par chaque segment
        mine = set()
        for i in range(len(nodes)):
            r = (lats[i] - g_min_lat) // cell_lat; c = (lons[i] - g_min_lon) // cell_lon
            mine.add((r, c))
            if i:
                r0 = (lats[i - 1] - g_min_lat) // cell_lat; c0 = (lons[i - 1] - g_min_lon) // cell_lon
                for rr in range(min(r0, r), max(r0, r) + 1):
                    for cc in range(min(c0, c), max(c0, c) + 1):
                        mine.add((rr, cc))
        for rc in mine:
            cells.setdefault(rc, []).append(wi)

        # Arcs du graphe : d'une jonction à la suivante le long du tronçon
        lim_f = mf or ms or default_for(hw) or 50
        lim_b = mb or ms or default_for(hw) or 50
        fac = FACTOR[hw]
        a = 0
        length = 0.0
        for i in range(1, len(nodes)):
            length += haversine(lats[i - 1] / 1e7, lons[i - 1] / 1e7, lats[i] / 1e7, lons[i] / 1e7)
            if nodes[i] in gid:
                tf = length / (lim_f * fac / 3.6) if oneway != -1 else -1.0
                tb = length / (lim_b * fac / 3.6) if oneway != 1 else -1.0
                ga, gb = gid[nodes[a]], gid[nodes[i]]
                segs += struct.pack("<iiiiifff", wi, pt_start + a, pt_start + i, ga, gb, length, tf, tb)
                seg_from.append(ga); seg_to.append(gb)
                n_seg += 1
                a = i
                length = 0.0

    # Adjacence (CSR) : chaque arc est rangé chez ses deux extrémités, avec son sens (0 = A→B)
    deg = array("i", [0]) * (n_g + 1)
    for s in range(n_seg):
        deg[seg_from[s]] += 1
        deg[seg_to[s]] += 1
    adj_start = array("i", [0]) * (n_g + 1)
    acc = 0
    for g in range(n_g):
        adj_start[g] = acc
        acc += deg[g]
    adj_start[n_g] = acc
    fill = array("i", adj_start[:n_g])
    adj = array("i", [0]) * acc
    for s in range(n_seg):
        a, b = seg_from[s], seg_to[s]
        adj[fill[a]] = s * 2; fill[a] += 1
        adj[fill[b]] = s * 2 + 1; fill[b] += 1

    # Interdictions de tourner (via un nœud de jonction)
    restr = []
    for frm, via, to, kind in rd.restrictions:
        if frm in way_index and to in way_index and via in gid:
            restr.append((gid[via], way_index[frm], way_index[to], kind))
    restr.sort()
    restr_b = b"".join(struct.pack("<iiii", *r) for r in restr)

    exits = sorted((gid[n], strings.ref(ref)) for n, ref in rd.exits.items() if n in gid)
    exits_b = b"".join(struct.pack("<ii", *e) for e in exits)

    grid_start = array("i", [0]) * (rows * cols + 1)
    grid_entries = array("i")
    for r in range(rows):
        for c in range(cols):
            grid_start[r * cols + c] = len(grid_entries)
            lst = cells.get((r, c))
            if lst:
                grid_entries.extend(lst)
    grid_start[rows * cols] = len(grid_entries)

    sections = [bytes(strings.blob), bytes(way_rec), pt_lat.tobytes(), pt_lon.tobytes(), pt_g.tobytes(),
                bytes(segs), adj_start.tobytes(), g_pt.tobytes(), adj.tobytes(), restr_b, exits_b,
                grid_start.tobytes(), grid_entries.tobytes()]
    assert sys.byteorder == "little"
    header_size = 128
    offsets = []
    pos = header_size
    for s in sections:
        offsets.append(pos)
        pos += len(s)
        pos += (-pos) % 4
    head = struct.pack("<4s17i", MAGIC, FORMAT, version, len(ways), len(pt_lat), n_seg, n_g, acc, len(restr),
                       len(exits), rows, cols, len(grid_entries), g_min_lat, g_min_lon, cell_lat, cell_lon,
                       len(strings.blob))
    head += struct.pack("<13i", *offsets)
    head += b"\0" * (header_size - len(head))
    with open(out, "wb") as f:
        f.write(head)
        for s, off in zip(sections, offsets):
            f.write(b"\0" * (off - f.tell()))
            f.write(s)
    print(f"roads.bin : {len(ways)} tronçons, {len(pt_lat)} points, {n_g} jonctions, {n_seg} arcs, "
          f"{len(restr)} interdictions, {len(exits)} sorties, grille {rows}×{cols}, {pos / 1e6:.1f} Mo", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("out")
    ap.add_argument("--version", type=int, default=0)
    a = ap.parse_args()
    build(a.pbf, a.out, a.version)
