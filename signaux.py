#!/usr/bin/env python3
"""Signaux faibles de prospection pour SuiviMarché -> signaux.json (lu via RELAIS_URL).

Trois sections, aucune clé obligatoire (bibliothèque standard uniquement) :
- presse : flux RSS de médias énergie / industrie, filtrés par mots-clés
  (solaire, ombrières, ISO 14001/50001, recrutements énergie/RSE, agrandissements...) ;
- emplois : API France Travail (api.francetravail.io) SI les variables d'environnement
  FT_ID et FT_SECRET sont renseignées (secrets GitHub du dépôt), offres « énergie »,
  « photovoltaïque », « RSE », « facility manager » des départements du périmètre,
  publiées dans les 7 derniers jours ; sinon section vide avec une note ;
- salons : événements à venir du secteur (date/lieu/lien vers la liste des exposants).

Le fichier est poussé tel quel ; _meta indique quelles sources ont répondu.
Lancement manuel : python signaux.py ; variables : SIGNAUX_JOURS (défaut 30), SIGNAUX_MAX (défaut 60).
"""

import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signaux.json")

JOURS = int(os.environ.get("SIGNAUX_JOURS", "30"))
MAX = int(os.environ.get("SIGNAUX_MAX", "60"))

RSS = [
    ("GreenUnivers", "https://www.greenunivers.com/feed/"),
    ("Actu-Environnement", "https://www.actu-environnement.com/rss/"),
    ("Les-EnR.eu", "https://www.les-energies-renouvelables.eu/feed/"),
    ("Révolution Énergétique", "https://www.revolution-energetique.com/feed/"),
]

MOTS = [
    "photovoltaïque", "photovoltaique", "solaire", "ombrière", "ombrières",
    "autoconsommation", "iso 14001", "iso 50001", "responsable énergie",
    "responsable rse", "chargé d'affaires solaire", "facility manager",
    "recrutement", "recrute", "agrandissement", "extension", "nouvelle usine",
    "raccordement", "enedis", "appel à projets", "appel à manifestation",
    "bilan ges", "décarbonation", "décarboner", "prime", "prime à l'autoconsommation",
]

SALONS = [
    {"nom": "Salon des Maires et des Collectivités", "dates": "novembre (annuel)", "lieu": "Paris Porte de Versailles",
     "url": "https://www.salondesmaires.com/", "exposants": "https://www.salondesmaires.com/liste-exposants/", "theme": "collectivités, énergie, bâtiment"},
    {"nom": "EnerJ Meeting Paris", "dates": "février (annuel)", "lieu": "Paris",
     "url": "https://www.enerj-meeting.com/paris", "exposants": "https://www.enerj-meeting.com/exposants", "theme": "efficacité énergétique et EnR du bâtiment"},
    {"nom": "EnerJ Meeting Lyon", "dates": "juin (annuel)", "lieu": "Lyon",
     "url": "https://www.enerj-meeting.com/lyon", "exposants": "https://www.enerj-meeting.com/exposants", "theme": "efficacité énergétique et EnR du bâtiment"},
    {"nom": "Pollutec", "dates": "décembre (biennal)", "lieu": "Lyon Eurexpo",
     "url": "https://www.pollutec.com/", "exposants": "https://www.pollutec.com/fr/exposants", "theme": "environnement, énergie, déchets"},
    {"nom": "BePositive", "dates": "mars (annuel)", "lieu": "Lyon Eurexpo",
     "url": "https://www.bepositive-event.com/", "exposants": "https://www.bepositive-event.com/", "theme": "énergies renouvelables, solaire"},
    {"nom": "Global Industrie", "dates": "mars (annuel)", "lieu": "Paris / Lyon",
     "url": "https://www.global-industrie.com/", "exposants": "https://www.global-industrie.com/", "theme": "industrie, sous-traitance, énergie"},
]

DEPS = ["02", "08", "51", "54", "59", "62", "80"]  # périmètre Nord France de SuiviMarché
# Jeton France Travail : serveur d'authentification entreprise.francetravail.fr (realm partenaire),
# identifiants dans le corps de la requête ; les API elles-mêmes restent sur api.francetravail.io.
FT_TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
FT_API = "https://api.francetravail.io"
SCOPES_LBB = "api_labonneboitev2 search office"
SCOPES = os.environ.get("FT_SCOPES", "api_offresdemploiv2 o2dsoffre " + SCOPES_LBB).strip()
# La Bonne Boîte v2 impose un métier ou un secteur : grands domaines ROME ciblés
# (H Industrie, N Transport et logistique, A Agriculture), hors intérim / placement (NAF 78).
LBB_DOMAINES = [x for x in os.environ.get("LBB_DOMAINES", "H,N,A").replace(" ", "").split(",") if x]
LBB_NAF_EXCLUS = ("78",)
LBB_PAR_DEP = 10

UA = {"User-Agent": "Mozilla/5.0 (compatible; SuiviMarche-relais/1.0; prospection signaux faibles)"}


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_rss(data):
    """Entrées RSS/Atom -> liste de dicts (titre, lien, date, resume)."""
    out = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    ns = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}
    items = root.findall(".//item") or root.findall(".//atom:entry", ns)
    for it in items:
        def g(tag):
            e = it.find(tag)
            if e is None:
                e = it.find("atom:" + tag, ns)
            return (e.text or "").strip() if e is not None and e.text else ""
        titre = g("title")
        lien = g("link")
        if not lien:
            e = it.find("atom:link", ns)
            lien = e.get("href", "") if e is not None else ""
        pub = g("pubDate") or g("published") or g("updated") or ""
        if not pub:
            e = it.find("dc:date", ns)
            pub = (e.text or "") if e is not None and e.text else ""
        resume = g("description") or g("summary")
        resume = re.sub(r"<[^>]+>", " ", resume)
        resume = html.unescape(resume)                      # entités HTML des flux (&egrave; &#8217;…)
        resume = re.sub(r"\s+", " ", resume).strip()[:300]
        out.append({"titre": html.unescape(titre), "url": lien, "date": parse_date(pub), "extrait": resume})
    return out


def parse_date(pub):
    """RFC822 (RSS) ou ISO (Atom) -> 'AAAA-MM-JJ' ou '' si illisible."""
    if not pub:
        return ""
    p = pub.strip()
    try:
        import email.utils
        d = email.utils.parsedate_to_datetime(p)
        if d:
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", p)
    return m.group(1) if m else ""


def date_ok(pub, jours):
    d = parse_date(pub)
    if not d:
        return True  # date illisible : le flux RSS est récent de toute façon
    return (dt.datetime.now(dt.timezone.utc).date() - dt.date.fromisoformat(d)).days <= jours


def section_presse(sources):
    entrees, etat = [], {}
    for nom, url in sources:
        try:
            items = parse_rss(fetch(url))
            etat[nom] = "ok (%d entrées lues)" % len(items)
            for it in items:
                texte = (it["titre"] + " " + it["extrait"]).lower()
                if not date_ok(it["date"], JOURS):
                    continue
                mots = [m for m in MOTS if m in texte]
                if not mots:
                    continue
                entrees.append({
                    "titre": it["titre"], "source": nom, "date": it["date"], "url": it["url"],
                    "mots": mots, "extrait": it["extrait"],
                })
        except Exception as e:
            etat[nom] = "erreur: %s" % str(e)[:120]
    # tri : date décroissante quand possible, puis titre
    entrees.sort(key=lambda x: x["date"], reverse=True)
    return entrees[:MAX], etat


def section_emplois(token, domaine, avec_offres):
    """API France Travail (offres d'emploi v2) si le jeton couvre ce scope."""
    if not avec_offres:
        return [], "API « Offres d'emploi v2 » non ajoutée à l'application francetravail.io (l'ajouter puis Enregistrer)"
    requetes = ["responsable énergie", "chargé d'affaires photovoltaïque", "photovoltaïque", "RSE", "facility manager", "énergie bâtiment"]
    out = []
    limite = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=7)).isoformat()
    try:
        for q in requetes:
            params = urllib.parse.urlencode({"q": q, "departement": ",".join(DEPS), "range": "0-39", "sort": "2"})
            req = urllib.request.Request(
                domaine + "/partenaire/offresdemploi/v2/offres/search?" + params,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                j = json.load(r)
            for o in (j.get("resultats") or []):
                d = (o.get("dateCreation") or "")[:10]
                if d and d < limite:      # filtre « 7 derniers jours » côté client
                    continue
                out.append({
                    "titre": html.unescape(o.get("intitule") or ""),
                    "entreprise": html.unescape((o.get("entreprise") or {}).get("nom") or ""),
                    "lieu": html.unescape((o.get("lieuTravail") or {}).get("libelle") or ""),
                    "date": d, "url": (o.get("origineOffre") or {}).get("url") or "", "mot": q,
                })
            if len(out) >= 40:
                break
        out = out[:40]
        return out, "ok (%d offres sur 7 jours)" % len(out)
    except Exception as e:
        return [], "erreur API France Travail: %s" % str(e)[:140]


def section_recrutent(token, domaine):
    """La Bonne Boîte v2 (route /recherche) : établissements à fort potentiel d'embauche par département
    du périmètre, dans les grands domaines LBB_DOMAINES. L'API ne publie ni téléphone ni site."""
    out = []
    try:
        for dep in DEPS:
            time.sleep(0.6)   # limite La Bonne Boîte v2 : 2 appels/s
            params = urllib.parse.urlencode({"granddomain": LBB_DOMAINES, "department_number": dep,
                                             "page_size": 100, "sort_by": "hiring_potential",
                                             "sort_direction": "desc"}, doseq=True)
            req = urllib.request.Request(
                domaine + "/partenaire/labonneboite/v2/recherche?" + params,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    j = json.load(r)
            except Exception as e:
                return [], "erreur La Bonne Boîte (%s): %s" % (dep, str(e)[:80])
            vu_dep, n = set(), 0
            for e in (j.get("items") or []):   # déjà triés par potentiel d'embauche décroissant
                siret = str(e.get("siret") or "")
                naf = str(e.get("naf") or "")
                if not siret or siret in vu_dep or naf.startswith(LBB_NAF_EXCLUS):
                    continue
                vu_dep.add(siret)
                nom = e.get("company_name") or e.get("office_name") or ""
                ens = e.get("office_name") or ""
                if ens and ens.upper() != nom.upper():
                    nom = "%s (%s)" % (nom, ens)
                hmin, hmax = e.get("headcount_min") or 0, e.get("headcount_max") or 0
                score = e.get("hiring_potential")
                out.append({
                    "siren": siret[:9], "siret": siret,
                    "nom": html.unescape(nom),
                    "adresse": html.unescape(" ".join(filter(None, [e.get("postcode"), e.get("city")]))),
                    "dep": e.get("department_number") or dep,
                    "taille": ("%d-%d" % (hmin, hmax)) if hmax else "",
                    "score": round(score) if isinstance(score, (int, float)) else None,
                    "naf": html.unescape(e.get("naf_label") or naf), "tel": "", "site": "",
                })
                n += 1
                if n >= LBB_PAR_DEP:
                    break
        # dédoublonne par SIRET, tri score décroissant
        vu, res = set(), []
        for x in sorted(out, key=lambda x: -(x.get("score") or 0)):
            if x["siret"] and x["siret"] not in vu:
                vu.add(x["siret"]); res.append(x)
        return res, "ok (%d entreprises)" % len(res)
    except Exception as e:
        return [], "erreur La Bonne Boîte: %s" % str(e)[:120]


def _ft_token(ft_id, ft_secret):
    """Jeton OAuth2 client_credentials (francetravail.io). Repli sur les scopes La Bonne Boîte seuls
    si l'application n'a pas encore l'API Offres d'emploi v2 -> retourne (token, domaine, scopes_ok)."""
    erreur = ""
    for scope in (SCOPES, SCOPES_LBB):
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": ft_id,
                                       "client_secret": ft_secret, "scope": scope}).encode()
        req = urllib.request.Request(FT_TOKEN_URL, data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)["access_token"], FT_API, scope
        except urllib.error.HTTPError as e:
            try:
                code = json.loads(e.read().decode("utf-8", "replace")).get("error") or ""
            except Exception:
                code = ""
            erreur = "HTTP %s %s" % (e.code, code)
            if code == "invalid_client":   # identifiants refusés : inutile d'essayer d'autres scopes
                raise RuntimeError("jeton France Travail refusé (invalid_client) : vérifier que FT_ID / FT_SECRET sont l'identifiant client et la clé secrète de l'application francetravail.io")
        except Exception as e:
            erreur = str(e)[:80]
    raise RuntimeError("jeton France Travail refusé (%s) : vérifier que les API La Bonne Boîte v2 / Offres d'emploi v2 sont ajoutées à l'application francetravail.io" % erreur.strip())


def main():
    presse, etat_presse = section_presse(RSS)
    emplois, etat_emplois = [], "API France Travail non configurée"
    recrutent, etat_recrutent = [], "API France Travail non configurée"
    ft_id = os.environ.get("FT_ID", "").strip()
    ft_secret = os.environ.get("FT_SECRET", "").strip()
    if ft_id and ft_secret:
        try:
            token, domaine, scope = _ft_token(ft_id, ft_secret)
            avec_offres = "offresdemploi" in scope
            emplois, etat_emplois = section_emplois(token, domaine, avec_offres)
            recrutent, etat_recrutent = section_recrutent(token, domaine)
        except Exception as e:
            etat_emplois = etat_recrutent = str(e)[:160]
    coords = {}
    for x in recrutent:
        if not x.get("siren"):
            continue
        c = coords.setdefault(x["siren"], {})
        for k in ("adresse", "tel", "site"):
            if x.get(k) and not c.get(k):
                c[k] = x[k]
        c["source"] = "La Bonne Boîte v2 (recrute)"
    doc = {
        "_meta": {
            "maj": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "jours": JOURS,
            "sources": {"presse": etat_presse, "emplois": etat_emplois, "recrutent": etat_recrutent},
            "note": "Signaux faibles : presse énergie/industrie filtrée, offres d'emploi énergie/RSE, entreprises qui recrutent (La Bonne Boîte), salons à venir. coords = coordonnées publiées par SIREN pour enrichir les fiches prospects.",
        },
        "presse": presse,
        "emplois": emplois,
        "recrutent": recrutent,
        "coords": coords,
        "salons": SALONS,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("signaux.json : %d presse, %d emplois, %d recrutent, %d salons -> %s" % (len(presse), len(emplois), len(recrutent), len(SALONS), OUT))
    print("presse:", etat_presse)
    print("emplois:", etat_emplois)
    print("recrutent:", etat_recrutent)


if __name__ == "__main__":
    main()
