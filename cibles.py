#!/usr/bin/env python3
"""Cibles de prospection PV par département pour SuiviMarché -> cibles/<dep>.json.

Principe (bibliothèque standard uniquement, aucune clé) :
1. API Recherche d'entreprises (recherche-entreprises.api.gouv.fr) : établissements actifs du
   département dont le code NAF figure dans les secteurs cibles (CIBLES ci-dessous) et dont
   l'effectif atteint le seuil du secteur ;
2. consommation électrique estimée = effectif médian de la tranche x ratio MWh/salarié du secteur
   (ordres de grandeur CEREN/ADEME, utiles pour trier, pas pour chiffrer une offre) ;
3. pour les CIBLES_TOITURE meilleurs établissements : emprise des bâtiments non résidentiels dans
   un rayon de ~100 m du point Sirene (BD TOPO IGN via le WFS de la Géoplateforme), convertie en
   kWc installables avec les hypothèses de prospects_bdnb.py (45 % de l'emprise, 5,5 m²/kWc) ;
4. croisement avec icpe/<dep>.json du relais (installation classée = site industriel confirmé) ;
4b. zone d'activité : polygone « Zone industrielle » de la BD TOPO (couche zone_d_activite_ou_d_interet,
   toponyme du type « Zone Industrielle de Seclin ») contenant le point ou à moins de ZONE_TOLERANCE_M,
   sinon zone lue dans l'adresse Sirene (ZI, ZA, ZAC, parc d'activités...) ; zonage PLU au point
   (API Carto IGN / Géoportail de l'urbanisme : libellé UE, UI, 1AUx... et intitulé long) ;
5. score 0-100 : consommation estimée (40, plafond atteint vers 1 000 MWh, malus au-delà de
   30 GWh), toiture (30), ICPE (10), effectif connu à l'établissement (10), PME/ETI (10).

Lancement : python cibles.py 59 [62 ...] ; variables : CIBLES_TOITURE (défaut 400 établissements
enrichis par département), CIBLES_SANS_PLU=1 (saute le zonage PLU, ~0,5 s par établissement), CIBLES_MAX (défaut 1 000 établissements conservés + CIBLES_INF = 300 sous 500 MWh), CIBLES_SANS_TOITURE=1.
"""

import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
API = "https://recherche-entreprises.api.gouv.fr/search"
WFS = "https://data.geopf.fr/wfs/ows"
UA = {"User-Agent": "SuiviMarche-cibles/1.0"}

N_TOITURE = int(os.environ.get("CIBLES_TOITURE", "400"))
N_MAX = int(os.environ.get("CIBLES_MAX", "1000"))
N_INF = int(os.environ.get("CIBLES_INF", "300"))   # quota supplémentaire d'établissements sous 500 MWh (potentiel inférieur)
CONSO_INF = 500
SANS_TOITURE = os.environ.get("CIBLES_SANS_TOITURE") == "1"
SANS_PLU = os.environ.get("CIBLES_SANS_PLU") == "1"
GPU = "https://apicarto.ign.fr/api/gpu/zone-urba"
ZONE_TOLERANCE_M = 120
ZONE_ADR = re.compile(r"\b(Z\.?I\.?|Z\.?A\.?[CEL]?|ZONE INDUSTRIELLE|ZONE D['’ ]?ACTIVITES?|ZONE ARTISANALE|ZONE COMMERCIALE|ZONE D['’ ]?AMENAGEMENT CONCERTE|PARC D['’ ]?ACTIVITES?|PARC INDUSTRIEL|PARC LOGISTIQUE|PARC D['’ ]?ENTREPRISES|PARC TERTIAIRE|PARC SCIENTIFIQUE|POLE D['’ ]?ACTIVITES?|ESPACE D['’ ]?ACTIVITES?|TECHNOPOLE|PLATEFORME MULTIMODALE|ACTIPARC|ACTIPOLE|ECOPARC)(?=\s|$)(.*?)(?=\s\d{5}\s|$)", re.I)
PLU_ECO = re.compile(r"activit|econom|industri|artisan|commerc|logisti|entrepris|tertiair", re.I)
PART_TOITURE, M2_PAR_KWC = 0.45, 5.5   # mêmes hypothèses que prospects_bdnb.py

# Effectif médian par tranche INSEE
EFFECTIF = {"00": 0, "01": 1, "02": 4, "03": 7, "11": 15, "12": 35, "21": 75, "22": 150, "31": 225,
            "32": 350, "41": 750, "42": 1500, "51": 3500, "52": 7500, "53": 10000}
TRANCHES = ["00", "01", "02", "03", "11", "12", "21", "22", "31", "32", "41", "42", "51", "52", "53"]

# (libellé, codes NAF ou préfixes, effectif minimal, ratio MWh électriques par salarié et par an)
CIBLES = [
    ("Agriculture, élevage, serres", ["01.1", "01.2", "01.3", "01.4", "01.5", "01.6"], 3, 25),
    ("Industrie agroalimentaire", ["10.", "11.0"], 10, 40),
    ("Entreposage frigorifique", ["52.10A"], 10, 90),
    ("Logistique, entreposage", ["52.10B", "52.29A", "52.29B", "49.41A", "49.41B", "49.41C"], 20, 10),
    ("Commerce de gros alimentaire et agricole", ["46.21Z", "46.31Z", "46.32A", "46.32B", "46.32C", "46.33Z", "46.38A", "46.38B", "46.39A", "46.39B"], 10, 20),
    ("Négoce de matériaux", ["46.73A", "46.73B", "46.74A"], 20, 6),
    ("Grandes surfaces", ["47.11D", "47.11F", "47.52B", "47.59B"], 20, 25),
    ("Textile, bois, papier, imprimerie", ["13.", "16.", "17.", "18.1"], 10, 45),
    ("Chimie, plastiques, caoutchouc", ["20.", "22."], 10, 90),
    ("Verre, céramique, ciment, béton", ["23."], 10, 100),
    ("Métallurgie, fonderie", ["24."], 10, 150),
    ("Travail des métaux, traitement de surface", ["25."], 10, 20),
    ("Équipements, machines, automobile", ["26.", "27.", "28.", "29.", "30."], 20, 12),
    ("Meubles, autres industries, réparation", ["31.", "32.", "33."], 20, 10),
    ("Carrières, déchets, recyclage", ["08.1", "38.1", "38.2", "38.3"], 10, 40),
    ("Concessions et garages", ["45.11Z", "45.19Z", "45.20A", "45.20B"], 20, 8),
    ("Hôtels, blanchisseries, centres sportifs", ["55.10Z", "96.01A", "93.11Z", "93.13Z"], 20, 15),
    ("Santé, hébergement médicalisé", ["86.10Z", "87.10A", "87.10B", "87.30A"], 50, 10),
    ("Centres de données", ["63.11Z"], 10, 200),
]
ICPE_BONUS = {"Autorisation": 10, "Enregistrement": 7}


def api(params, essais=3):
    url = API + "?" + urllib.parse.urlencode(params)
    for i in range(essais):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * (i + 1)); continue
            print("  API %s : %s" % (e.code, e.read().decode("utf-8", "replace")[:200]), file=sys.stderr)
            return None
        except Exception:   # coupure réseau, délai dépassé, réponse tronquée
            time.sleep(3 * (i + 1))
    return None


def charger_naf():
    try:
        with open(os.path.join(ICI, "naf.json"), encoding="utf-8") as f:
            codes = json.load(f).get("codes") or []
        return {c[0]: c[1] for c in codes if isinstance(c, (list, tuple)) and len(c) >= 2}
    except Exception:
        return {}


def charger_icpe(dep):
    try:
        with open(os.path.join(ICI, "icpe", "%s.json" % dep), encoding="utf-8") as f:
            lst = json.load(f).get("donnees") or []
    except Exception:
        return {}
    par_siret = {}
    for x in lst:
        s = (x.get("siret") or "").strip()
        if len(s) == 14:
            par_siret.setdefault(s, []).append(x.get("regime") or "")
    return par_siret


def secteur_de(naf):
    for i, (lib, prefixes, eff_min, ratio) in enumerate(CIBLES):
        if any(naf.startswith(p) for p in prefixes):
            return i, lib, eff_min, ratio
    return None


def codes_secteur(prefixes, naf_libs):
    """codes NAF complets (xx.xxX) couverts par les préfixes, pour le paramètre activite_principale"""
    out = []
    for p in prefixes:
        if len(p) == 6:
            out.append(p)
        else:
            out += [c for c in naf_libs if c.startswith(p)]
    return sorted(set(out))


def tranches_min(eff_min):
    return [t for t in TRANCHES if EFFECTIF[t] >= eff_min and t != "00"]


def etablissements(dep, naf_libs):
    """établissements actifs du département par secteur cible (requêtes par paquets de codes NAF)"""
    vus, out = set(), []
    for idx, (lib, prefixes, eff_min, ratio) in enumerate(CIBLES):
        codes = codes_secteur(prefixes, naf_libs) or prefixes
        tr = ",".join(tranches_min(eff_min))
        for k in range(0, len(codes), 8):
            paquet = codes[k:k + 8]
            page, pages = 1, 1
            while page <= pages and page <= 400:
                d = api({"activite_principale": ",".join(paquet), "departement": dep, "tranche_effectif_salarie": tr,
                         "etat_administratif": "A", "per_page": 25, "page": page, "minimal": "true",
                         "include": "siege,finances,matching_etablissements"})
                if not d:
                    break
                pages = d.get("total_pages") or 1
                for ul in d.get("results") or []:
                    eff_ul = ul.get("tranche_effectif_salarie") or ""
                    n_etab = ul.get("nombre_etablissements_ouverts") or 1
                    fin = ul.get("finances") or {}
                    annee = max(fin) if fin else None
                    ca = (fin.get(annee) or {}).get("ca") if annee else None
                    for e in ul.get("matching_etablissements") or []:
                        naf = e.get("activite_principale") or ""
                        if e.get("etat_administratif") != "A" or not naf.replace(".", "")[:2].isdigit():
                            continue
                        if (e.get("code_postal") or "")[:len(dep)] != dep and (e.get("commune") or "")[:len(dep)] != dep:
                            continue
                        if naf not in paquet and not any(naf.startswith(p) for p in prefixes):
                            continue
                        siret = e.get("siret")
                        if not siret or siret in vus:
                            continue
                        vus.add(siret)
                        tr_e = e.get("tranche_effectif_salarie") or ""
                        connu = tr_e in EFFECTIF and tr_e != "00"
                        # effectif retenu : celui de l'établissement, sinon celui de l'unité légale réparti sur ses établissements
                        if connu:
                            eff = EFFECTIF[tr_e]
                        else:
                            eff = (EFFECTIF.get(eff_ul, 0) / max(n_etab, 1)) if eff_ul in EFFECTIF else 0
                        if eff < eff_min:
                            continue
                        out.append({
                            "siren": ul.get("siren"), "siret": siret, "nom": ul.get("nom_complet") or ul.get("nom_raison_sociale"),
                            "enseigne": (e.get("liste_enseignes") or [None])[0] or e.get("nom_commercial"),
                            "naf": naf, "secteur": lib,
                            "effectif": tr_e if connu else "", "eff_est": round(eff),
                            "siege": bool(e.get("est_siege")), "n_etab": n_etab,
                            "categorie": ul.get("categorie_entreprise") or "", "ca": ca, "ca_annee": annee,
                            "adresse": e.get("adresse") or "", "com": (e.get("libelle_commune") or "").title(),
                            "lat": _f(e.get("latitude")), "lon": _f(e.get("longitude")),
                            "creation": e.get("date_creation"),
                            "conso_mwh": round(eff * ratio),
                        })
                page += 1
                time.sleep(0.15)   # 7 requêtes/s autorisées
        print("  %-45s %5d établissements" % (lib, sum(1 for x in out if x["secteur"] == lib)), flush=True)
    return out


def _f(v):
    try:
        return round(float(v), 5)
    except (TypeError, ValueError):
        return None


def toiture(lat, lon, rayon_m=100):
    """emprise (m²) des bâtiments non résidentiels de la BD TOPO dans un carré de +/- rayon autour du point"""
    if lat is None or lon is None:
        return None
    dlat = rayon_m / 111320.0
    dlon = rayon_m / (111320.0 * math.cos(math.radians(lat)))
    q = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": "BDTOPO_V3:batiment",
         "OUTPUTFORMAT": "application/json", "SRSNAME": "EPSG:2154", "COUNT": 300,
         "BBOX": "%f,%f,%f,%f,CRS:84" % (lon - dlon, lat - dlat, lon + dlon, lat + dlat)}
    try:
        with urllib.request.urlopen(urllib.request.Request(WFS + "?" + urllib.parse.urlencode(q), headers=UA), timeout=40) as r:
            fc = json.load(r)
    except Exception:
        return None
    total = 0.0
    for f in fc.get("features") or []:
        p = f.get("properties") or {}
        usage = (p.get("usage_1") or "") + " " + (p.get("nature") or "")
        if "ésidentiel" in usage or "Annexe" in usage:
            continue
        total += _aire(f.get("geometry"))
    return round(total)


def _aire(g):
    if not g:
        return 0.0
    t, c = g.get("type"), g.get("coordinates")
    if t == "Polygon":
        return _shoelace(c[0]) if c else 0.0
    if t == "MultiPolygon":
        return sum(_shoelace(p[0]) for p in c if p)
    return 0.0


def _shoelace(ring):
    a = 0.0
    for i in range(len(ring) - 1):
        a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(a) / 2


def zones_bdtopo(dep):
    """polygones « Zone industrielle » de la BD TOPO du département : [(toponyme, [anneaux lon/lat])]"""
    out, start = [], 0
    while True:
        q = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": "BDTOPO_V3:zone_d_activite_ou_d_interet",
             "OUTPUTFORMAT": "application/json", "COUNT": 1000, "STARTINDEX": start, "SRSNAME": "CRS:84",
             "CQL_FILTER": "nature='Zone industrielle' AND insee_commune LIKE '%s%%'" % dep}
        try:
            with urllib.request.urlopen(urllib.request.Request(WFS + "?" + urllib.parse.urlencode(q), headers=UA), timeout=120) as r:
                fc = json.load(r)
        except Exception as ex:
            print("  zones BD TOPO indisponibles : %s" % ex, file=sys.stderr)
            break
        feats = fc.get("features") or []
        for f in feats:
            p = f.get("properties") or {}
            g = f.get("geometry") or {}
            rings = [poly[0] for poly in g.get("coordinates", [])] if g.get("type") == "MultiPolygon" else ([g["coordinates"][0]] if g.get("type") == "Polygon" else [])
            nom = (p.get("toponyme") or "").strip() or "Zone industrielle (%s)" % (p.get("commune") or "").title()
            if rings:
                out.append((nom, rings))
        if len(feats) < 1000:
            break
        start += 1000
    print("  zones industrielles BD TOPO : %d" % len(out), flush=True)
    return out


def _dans(lon, lat, ring):
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def zone_de(lat, lon, zones):
    """nom de la zone industrielle contenant le point, sinon la plus proche à moins de ZONE_TOLERANCE_M (distance aux sommets)"""
    if lat is None or lon is None:
        return ""
    kx = 111320.0 * math.cos(math.radians(lat))
    meilleure, dmin = "", ZONE_TOLERANCE_M
    for nom, rings in zones:
        for ring in rings:
            xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
            if lon < min(xs) - 0.003 or lon > max(xs) + 0.003 or lat < min(ys) - 0.002 or lat > max(ys) + 0.002:
                continue
            if _dans(lon, lat, ring):
                return nom
            d = min(math.hypot((p[0] - lon) * kx, (p[1] - lat) * 111320.0) for p in ring)
            if d < dmin:
                meilleure, dmin = nom, d
    return meilleure


def zone_adresse(adresse):
    """zone d'activité lue dans l'adresse Sirene : « ZI DE SECLIN », « PARC D'ACTIVITES DU MOULIN »..."""
    m = ZONE_ADR.search(adresse or "")
    if not m:
        return ""
    txt = re.sub(r"\s+", " ", (m.group(1) + " " + m.group(2)).strip())[:60].strip(" ,-")
    t = txt.title()
    for a, b in (("D'", "d'"), (" D ", " d'"), ("D’", "d'"), (" De ", " de "), (" Du ", " du "), (" Des ", " des "), (" La ", " la "), (" Le ", " le "), (" Les ", " les "), (" Et ", " et ")):
        t = t.replace(a, b)
    return re.sub(r"^(Z\.?[IA]\.?[CEL]?)(?=\s|$)", lambda m: m.group(1).upper(), t, flags=re.I)


def plu(lat, lon):
    """zonage PLU au point (Géoportail de l'urbanisme via l'API Carto IGN) : {z, lib, t}"""
    if lat is None or lon is None:
        return None
    g = json.dumps({"type": "Point", "coordinates": [lon, lat]})
    for i in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(GPU + "?" + urllib.parse.urlencode({"geom": g}), headers=UA), timeout=40) as r:
                feats = (json.load(r).get("features") or [])
            if not feats:
                return None
            p = feats[0].get("properties") or {}
            return {"z": p.get("libelle") or "", "lib": (p.get("libelong") or "")[:120], "t": p.get("typezone") or ""}
        except urllib.error.HTTPError as ex:
            if ex.code in (429, 500, 502, 503, 504):
                time.sleep(2 + 3 * i); continue
            return None
        except Exception:
            time.sleep(2)
    return None


def score(e):
    s = 0.0
    c = e["conso_mwh"]
    if c > 0:
        s += min(40.0, 40 * (math.log10(c) - 1.5) / 1.5)   # 30 MWh -> 0, 1 000 MWh et plus -> 40
    if c > 30000:
        s -= 10                                             # très gros sites (HTB, quotas CO2) : décision hors région
    if e.get("kwc"):
        s += min(30.0, 30 * (math.log10(e["kwc"]) - 1.5) / 1.5)          # 36 kWc -> ~1, 1 000 kWc -> 30
    s += e.get("icpe_pts", 0)
    if e["effectif"]:
        s += 10
    if e["categorie"] in ("PME", "ETI"):
        s += 10
    elif e["siege"]:
        s += 5
    return max(0, round(s))


def traiter(dep, naf_libs):
    print("Département %s" % dep, flush=True)
    etabs = etablissements(dep, naf_libs)
    icpe = charger_icpe(dep)
    for e in etabs:
        reg = icpe.get(e["siret"]) or []
        e["icpe"] = max(reg, key=lambda r: ICPE_BONUS.get(r, 3)) if reg else ""
        e["icpe_pts"] = max((ICPE_BONUS.get(r, 3) for r in reg), default=0)
    etabs.sort(key=lambda e: -e["conso_mwh"])
    if not SANS_TOITURE:
        n = 0
        for e in etabs[:N_TOITURE]:
            m2 = toiture(e["lat"], e["lon"])
            if m2 is not None:
                e["toiture_m2"] = m2
                e["kwc"] = round(m2 * PART_TOITURE / M2_PAR_KWC)
                n += 1
            time.sleep(0.1)
        print("  toitures mesurées : %d" % n, flush=True)
    for e in etabs:
        e["score"] = score(e)
        e.pop("icpe_pts", None)
    etabs.sort(key=lambda e: (-e["score"], -e["conso_mwh"]))
    garde = etabs[:N_MAX] + [e for e in etabs[N_MAX:] if e["conso_mwh"] < CONSO_INF][:N_INF]
    zones = zones_bdtopo(dep)
    n_zone = n_adr = n_plu = 0
    for e in garde:
        z = zone_de(e["lat"], e["lon"], zones)
        if z:
            e["zone"], e["zone_src"] = z, "bdtopo"; n_zone += 1
        else:
            za = zone_adresse(e["adresse"])
            if za:
                e["zone"], e["zone_src"] = za, "adresse"; n_adr += 1
        if not SANS_PLU:
            p = plu(e["lat"], e["lon"])
            if p:
                e["plu"] = p; n_plu += 1
                if PLU_ECO.search(p["lib"]) or re.match(r"^(\d?AU)?U?[EIXYZ]", p["z"] or ""):
                    e["plu_eco"] = True
            time.sleep(0.05)
    print("  zones : %d par la BD TOPO, %d par l'adresse ; PLU renseigné : %d" % (n_zone, n_adr, n_plu), flush=True)
    zones_n = {}
    for e in garde:
        if e.get("zone"):
            zones_n[e["zone"]] = zones_n.get(e["zone"], 0) + 1
    secteurs = {}
    for e in etabs:
        secteurs[e["secteur"]] = secteurs.get(e["secteur"], 0) + 1
    doc = {
        "dep": dep, "maj": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "API Recherche d'entreprises (DINUM/INSEE), BD TOPO IGN (Géoplateforme : bâtiments, zones d'activité), Géoportail de l'urbanisme (API Carto IGN), ICPE (Géorisques) via le relais",
        "hypotheses": {"effectif_median_tranche": True, "ratios_mwh_salarie": {c[0]: c[3] for c in CIBLES},
                       "part_toiture": PART_TOITURE, "m2_par_kwc": M2_PAR_KWC, "rayon_toiture_m": 100, "zone_tolerance_m": ZONE_TOLERANCE_M},
        "total": len(etabs), "secteurs": secteurs, "zones": dict(sorted(zones_n.items(), key=lambda kv: -kv[1])),
        "donnees": garde,
    }
    os.makedirs(os.path.join(ICI, "cibles"), exist_ok=True)
    with open(os.path.join(ICI, "cibles", "%s.json" % dep), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print("  %d établissements, %d conservés -> cibles/%s.json" % (len(etabs), len(garde), dep), flush=True)
    return doc


def main():
    deps = [a for a in sys.argv[1:] if a.isalnum()] or ["59"]
    naf_libs = charger_naf()
    for dep in deps:
        traiter(dep.zfill(2) if dep.isdigit() else dep.upper(), naf_libs)


if __name__ == "__main__":
    main()
