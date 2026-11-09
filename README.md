# suivimarche-data — relais de données pour SuiviMarché

Dépôt public alimenté chaque matin par GitHub Actions (`relais.py`, bibliothèque standard Python, aucune clé d'API). L'outil [SuiviMarché](https://github.com/GenerationsRenouvelables/suivimarche) lit ces fichiers depuis `raw.githubusercontent.com/GenerationsRenouvelables/suivimarche-data/main/`.

| Fichier | Contenu | Source |
|---|---|---|
| `spot.json` | prix spot day-ahead France par mois depuis 2015 (prix moyen, prix 11 h-15 h, **prix capté solaire** pondéré par la production solaire nationale, heures à prix négatif, min/max, profil horaire) et par jour sur 92 jours | [energy-charts.info](https://energy-charts.info) (Fraunhofer ISE, données ENTSO-E / EPEX), CC BY 4.0 |
| `jorf.json` | textes du Journal officiel des 24 derniers mois dont le titre concerne le solaire, l'autoconsommation ou l'électricité renouvelable (`tag` = `pv` ou `energie`), avec lien Légifrance `https://www.legifrance.gouv.fr/jorf/id/<id>` | [DILA, open data JORF](https://echanges.dila.gouv.fr/OPENDATA/JORF/), licence ouverte |
| `etat.json` | horodatages, dernière archive JORF traitée, erreurs éventuelles | |
| `naf.json` | nomenclature d'activités NAF rév. 2 (2008) : 21 sections, 88 divisions (avec leur section), 272 groupes, 732 sous-classes `[code, libellé]` — fichier statique lu par l'onglet « Par activité » des comptes clés de SuiviMarché (recherche d'entreprises par code APE) | [INSEE, listes NAF rév. 2](https://www.insee.fr/fr/information/2120875) |

## Fonctionnement

- Spot : chaque exécution recalcule le mois courant et le mois précédent (les mois plus anciens sont conservés) et les 92 derniers jours. Prix horaires jusqu'en septembre 2025, puis au quart d'heure.
- JORF : les archives incrémentales publiées depuis la dernière exécution sont lues (`texte/version/JORF/*.xml`, champ `TITREFULL`) ; les nominations et actes de gestion sont exclus.
- Le workflow `relais` se lance à 05:40 UTC ; l'entrée `full` du lancement manuel force un recalcul complet.

Exécution locale : `python relais.py` (variables `RELAIS_FULL`, `RELAIS_SPOT_DEBUT`, `RELAIS_JORF_JOURS`).

## Résumé BDNB par département (`bdnb.json`)

Workflow `bdnb` (GitHub Actions, déclenchement manuel ou le 5 de chaque mois) : pour chaque département demandé (ou absent du fichier, ou d'un autre millésime), il télécharge l'export départemental de la BDNB (CSTB, `open-data.s3.fr-par.scw.cloud`, 0,3 à 1,5 Go), exécute `prospects_bdnb.py --relais bdnb.json --sans-excel --nettoyer` et pousse le résultat après chaque département (jusqu'à 12 par run, délai 5 h 40).

`bdnb.json` = `{ _meta: {maj, millesime, hypotheses, departements}, deps: { "59": {millesime, calcule, batiments, score60, score80, avec_proprietaire, proteges, hta, chantier_recent, emprise_m2, kwc, mwh_an, conso_pro_mwh, secteurs, kwc_secteurs, communes[5], proprietaires[5], permis?} } }` — bâtiments professionnels d'emprise ≥ 400 m² et de potentiel > 36 kWc, score 0-100 (toiture, adéquation consommation, propriétaire identifié, HTA, chantier récent, contraintes patrimoniales). Lu par la page « Où prospecter » de SuiviMarché (composante « gisement bâti » du score et argumentaire).

## Cibles de prospection par département (`cibles/<dep>.json`)

Workflow `cibles` (déclenchement manuel ou le 6 de chaque mois, `cibles.py`, aucune clé) : établissements actifs du département dans les secteurs consommateurs d'électricité (agriculture et élevage, agroalimentaire, logistique et froid, grandes surfaces, industrie, carrières et déchets, concessions, hôtels, santé, centres de données ; codes NAF et seuils d'effectif dans `CIBLES`), lus par l'API Recherche d'entreprises (DINUM/INSEE). Pour chacun : consommation électrique **estimée** (effectif médian de la tranche × ratio MWh/salarié du secteur, ordres de grandeur CEREN/ADEME), emprise des bâtiments non résidentiels de la BD TOPO IGN dans un rayon de 100 m du point Sirene pour les 400 plus gros consommateurs (→ kWc installables, hypothèses de `prospects_bdnb.py`), installation classée du relais (`icpe/`), chiffre d'affaires et catégorie (PME/ETI/GE), score 0-100. Les 600 meilleurs par département sont publiés.

`cibles/<dep>.json` = `{ dep, maj, source, hypotheses, total, secteurs{}, donnees[] }` avec par établissement `siren, siret, nom, enseigne, naf, naf_lib, secteur, effectif (tranche établissement), effectif_ul, eff_est, siege, n_etab, categorie, ca, ca_annee, adresse, com, insee, lat, lon, creation, conso_mwh, toiture_m2?, kwc?, icpe, score`. Les consommations et toitures sont des estimations de tri, pas des données mesurées.

`prospects_bdnb.py` est le même fichier que dans le dépôt SuiviMarché (il y produit en plus les Excel `prospects_<dep>.xlsx` en local) : modifier les deux ensemble.
