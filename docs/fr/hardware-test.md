# Premier test terrain : commande `C`, caméra, pilote automatique sur carte

English: [docs/hardware-test.md](../hardware-test.md)

Une checklist numérotée pour la première séance sur du vrai matériel : un coffret Carrera Digital 124 avec 4 voitures.
Elle valide trois choses qui n'ont tourné jusqu'ici que sur la CU virtuelle et des images de synthèse :

1. la commande `C` de la CU d'origine en Bluetooth AppConnect (sans flasher de firmware) ;
2. la localisation par caméra (`trackloc`, un iPhone via Continuity Camera) ;
3. le pilote automatique qui conduit depuis la carte (le profil **Vitesse en virage (carte)**).

À faire dans cet ordre : chaque partie suppose la précédente réussie. Tout se fait depuis les deux pages web
(RaceLab sur `http://127.0.0.1:8130/`, trackloc sur l'adresse affichée par `serve`) ; la ligne de commande n'est que le
plan B, dans les lignes *Plan B*. Coche les cases et note les chiffres pour le rapport (partie 10).

**Règles pour toute la journée**

- Une main sur **STOP** dans RaceLab (ou `Échap`), l'autre à portée de l'alimentation de la piste. Le STOP de
  trackloc ne fait que terminer *son* action et rendre les voitures : ce n'est pas un arrêt d'urgence.
- Ne tiens jamais une manette sur un slot que le logiciel pilote, sauf pour le test sur cales de l'étape 1.
- Une seule chose bouge à la fois. Si quelque chose surprend : STOP, note l'heure, garde le journal (Système → Journal),
  et ne continue que quand tu as compris.
- Enregistre la vidéo pendant chaque test en mouvement à partir de l'étape 4 (*Enregistrer* de trackloc, ou un
  *Lancer* avec `--record`). Un mauvais résultat avec enregistrement s'analyse à la maison ; sans, ce n'est qu'une anecdote.

## 0. Avant de partir, à la maison

- [ ] Dépôt à jour, environnement virtuel activé dans **chaque** terminal :
  `source .venv/bin/activate` (`python -m trackloc` et `racelab` en ont besoin).
- [ ] `racelab --sim --stock-firmware --no-browser` démarre, la page s'ouvre, Système → Test matériel montre ses
  cinq étapes. Quitte-le.
- [ ] `cd racelab && ../.venv/bin/python -m pytest -q` et `cd trackloc && ../.venv/bin/python -m pytest -q` passent.
- [ ] Permission caméra donnée au terminal utilisé (Réglages Système → Confidentialité et sécurité → Caméra).
  Vérification : `python -m trackloc probe` liste l'iPhone (Continuity Camera demande l'iPhone déverrouillé, à
  proximité, même identifiant Apple, Wi-Fi et Bluetooth activés) avec une vraie résolution et un vrai nombre d'images/s.
- [ ] Marqueur imprimé : `python -m trackloc marker` écrit `trackloc_marker.pdf`. Imprime à **100 %** (pas « ajuster à
  la page »), papier mat, collé à plat sur une planche. **Mesure le carré noir à la règle** et note-le en
  millimètres (nominal 150) : les mètres de la carte en dépendent.
- [ ] Dans le sac : mètre ruban, ruban de masquage, chronomètre, trépied de téléphone ou support rigide (1,5 à 2,5 m
  au-dessus de la piste), chargeurs, piles de rechange pour les manettes. Mac, iPhone et voitures chargés.
- [ ] La CU est en firmware d'origine V338 ou V339 (Système → Connexion l'affiche une fois connectée). Plus ancien :
  mets-la à jour depuis Système → Options avancées à la maison, pas sur la piste.

## 1. Sécurité sur cales, voiture en l'air

Roues en l'air (une petite boîte sous le châssis), alimentation de la piste branchée. Lance `racelab --ble
--no-browser`, ouvre la page, choisis la CU dans Système → Connexion, passe en **Avec bots**.

- [ ] **Arrêt d'urgence.** Lance la voiture à un gaz bas (une impulsion du Test matériel, ou un bot Manuel à 3). Appuie
  sur **STOP**, puis refais avec `Échap`. RÉUSSI : les roues s'arrêtent net les deux fois, et rien ne repart quand
  tu lèves le STOP.
- [ ] **Homme mort.** Bot à 3, puis `kill -9` du processus RaceLab depuis un autre terminal (un `Ctrl-C` propre rend
  tout seul, seul un kill teste le watchdog de la CU). RÉUSSI : en environ 1 s la voiture revient sur sa manette
  (sans manette branchée, elle s'arrête).
- [ ] **Coupure Bluetooth.** Bot à 3, puis coupe le Bluetooth du Mac (ou la CU). RÉUSSI : la voiture s'arrête ou
  revient sur sa manette en environ 1 s, RaceLab affiche le lien perdu. Rallume : RaceLab se reconnecte et les bots
  restent à 0 tant que tu n'agis pas (ils ne reprennent jamais tout seuls).
- [ ] Facultatif : le bouton de changement de voie sur une aiguille fait un seul changement par appui.

**NO-GO** : l'un des trois ne stoppe pas la voiture. Pas de test sur piste. Note la version de la CU et le journal.

## 2. La commande `C` : Système → Test matériel

Une voiture (slot 1) sur une **ligne droite**, les autres hors de la piste, pas de séance, bots activés. La page a une
barre d'avertissement fixe avec son propre STOP. Fais les étapes dans l'ordre. À quoi ressemble RÉUSSI :

- [ ] **1. Vérification du lien** : transport Bluetooth AppConnect, version de la CU, transport des bots `C`, bots
  pris en charge. RÉUSSI : la ligne « la CU est connectée et prend la commande C d'origine ». Si elle dit B ou aucun :
  arrête, tu ne testes pas `C`.
- [ ] **2. Latence** : 100 aller-retour, puis 300. RÉUSSI (le panneau tranche) : rien de perdu, **p95 au plus 150 ms**,
  aucun aller-retour au-dessus de 400 ms. **Note le p95, la médiane, le max et le p95 d'acquittement de `C`** : c'est
  le chiffre BLE qu'on veut de cette journée. En cas d'échec, rapproche le Mac de la CU et mesure à nouveau ; deux
  échecs = NO-GO pour les bots en Bluetooth (la règle des 750 ms de la CU ne laisse pas de marge).
- [ ] **3. Impulsion de gaz** : slot 1, gaz 3, 1 s. RÉUSSI : le panneau dit que le lien a appliqué le gaz, puis 0, puis
  rendu le slot, **et tu réponds Oui** (la voiture a bougé, puis s'est arrêtée). Refais à 4 et à 6 (6 est le plafond :
  le serveur refuse 7 et plus, et les durées hors de 0,2 à 2 s, il ne tronque jamais). Si elle ne bouge pas à 3, monte
  d'un cran et note le plus petit gaz qui la fait bouger.
- [ ] **4. Changement de voie** : la même impulsion plus une demande de changement de voie, lancée juste avant une
  aiguille. RÉUSSI : Oui, la voiture a changé de voie à l'aiguille suivante.
- [ ] **5. Rendu (watchdog de la CU)** : aucune manette tenue sur ce slot, tous les autres slots libres de bots.
  RaceLab tient un gaz, puis cesse de rafraîchir la trame `C` pendant 1,5 s. RÉUSSI : la voiture s'arrête (ou sa
  manette reprend) environ 0,75 s après le début de la pause, soit **entre environ 0,5 s et 1,0 s**, et tu réponds
  Oui. Le panneau ne peut pas le mesurer : filme la voiture et l'écran avec l'iPhone au ralenti, ou fais annoncer
  l'arrêt chronomètre en main. Fais-le deux fois. Si la voiture continue pendant les 1,5 s, le watchdog de la CU
  n'agit pas : NO-GO pour les bots, signale-le.
- [ ] **6. Rapport** : note la piste, la voiture, la distance Bluetooth et tout détail curieux dans Notes, puis
  **Copier le rapport**. Il est aussi enregistré dans le dossier de données (`hardware-tests/hwtest-<date>.json`,
  sous `~/.racelab` par défaut).
- [ ] Une fois : appuie sur STOP pendant un test. RÉUSSI : le slot est mis à 0 et rendu, la pastille dit Interrompu.

## 3. Visée de la caméra : `trackloc serve`

Monte l'iPhone à 1,5 à 2,5 m au-dessus de la piste, regardant vers le bas (40 à 90 degrés conviennent), voyant toute
la piste avec de la marge. Pose le marqueur à plat sur la table, dans le champ, à côté de la piste. Verrouille
l'exposition, la mise au point et la balance des blancs sur l'iPhone (appui long sur l'image jusqu'à AE/AF LOCK), et
une cadence fixe de 30 ou 60 images/s.

Démarre RaceLab d'abord (connecté), puis la page, pour que la carte du connecteur apparaisse :

```bash
python -m trackloc serve --camera iphone --port 8140     # ouvre l'adresse affichée (elle finit par ?k=...)
```

L'en-tête du panneau Piste de RaceLab et Système → Connecteurs montrent une pastille « Camera (trackloc) » avec un
lien **Ouvrir** vers la page (l'adresse affichée marche aussi). Dans trackloc : **Ouvrir la caméra**, saisis la
**taille du marqueur** en millimètres (ta valeur mesurée).

- [ ] L'aperçu dit « marqueur OK » et montre la vraie résolution et cadence (note-les ; 1280x720 à 30 images/s suffit).
- [ ] Couverture : toute la piste est dans l'image, aucun bord ne la coupe.
- [ ] Pas d'avertissement « toute l'image change » (exposition, mise au point et blancs verrouillés).
- [ ] Rigide : l'image ne bouge pas quand tu tapes sur la table.
- [ ] Pas de bandes de lumière qui défilent (scintillement) : change ou éteins l'éclairage sinon.
- [ ] Pas de forte réflexion sur les rails, pas de câble ni de main au-dessus de la piste.
- [ ] Le marqueur fait plus d'environ 50 px sur l'image, il est à plat et hors de la piste.

*Plan B* : `python -m trackloc preview --camera iphone`, puis `http://127.0.0.1:8140/` (ou `--snapshot aim.jpg`).

## 4. D'abord enregistrer

Avant de cartographier quoi que ce soit, enregistre 2 minutes avec des voitures **conduites à la main** (manettes,
rien de piloté par le logiciel) : ça ne coûte rien et donne les premières vraies images, avec les passages de la CU
sur la même horloge.

- [ ] Page trackloc : **Enregistrer**, 120 s. Conduis 1 voiture, puis 2, quelques tours chacune.
- [ ] Le dossier `~/.racelab/trackloc/serve/<date>-record/` existe avec `video.mkv`, `frames.jsonl`, `inputs.jsonl`,
  `racelab.jsonl`, `meta.json` ; la vidéo se lit et les voitures y sont visibles.

*Plan B* : `python -m trackloc record --camera iphone --duration 120`.

## 5. Cartographier, puis comparer au mètre ruban

Mesure d'abord la vraie longueur de la ligne centrale de la voie 1 au mètre ruban (suis la rainure sur tout le tour,
deux fois) et note-la en mètres. Dégage la piste : aucune voiture garée sauf celle qui cartographie, pas de séance.

- [ ] Page trackloc : **Cartographier la piste**, voiture 1, gaz 4 (la page ne dépasse jamais 8 sur 15), deux voies, fond
  médiane (choisis « piste vide » si on ne peut pas éviter les mains ou les voitures garées), taille du marqueur
  renseignée. Confirme la boîte de dialogue, garde une main sur STOP dans RaceLab. RaceLab conduit la voiture à un
  gaz bas, une voie puis l'autre ; un calage monte le gaz d'un cran, jamais au-dessus du plafond. La voiture est
  toujours rendue (fin, STOP, erreur).
- [ ] Lis le résumé de la carte : longueur, voies, zones aveugles, comment les mètres ont été obtenus.
- [ ] **GO si la longueur de la carte est à moins de 2 % du mètre ruban** (8,00 m mesurés : 7,84 à 8,16 m). Hors de
  là : ne lance pas. Vérifie la taille du marqueur et le ruban, puis cartographie à nouveau. Note les deux chiffres.
- [ ] La forme dans RaceLab (panneau Piste) ressemble à ta piste.
- [ ] Note les zones aveugles (pont, ombre) : la détection devrait y baisser.

Si la carte échoue, garde le dossier de la tentative (`<date>-map/`) et envoie-le ; ne règle pas à l'aveugle.

*Plan B* : `python -m trackloc map --camera iphone --slot 0 --marker-size 0.150` (`--track-length-m <ruban>` remet les
mètres à l'échelle du ruban : dernier recours, et note que tu l'as utilisé).

## 6. Nommer les voitures (appel)

Gare les voitures utilisées, à une main les unes des autres, chacune sur son slot. Bots activés, pas de séance, STOP
sous la main.

- [ ] Page trackloc : **Nommer les voitures**, coche les slots présents. Chacune reçoit une impulsion basse (4 sur
  15, 0,5 s) et la voiture qui a bougé est ce slot.
- [ ] **GO si chaque voiture nommée est la bonne** (vérifie à l'œil que la voiture qui bouge est celle du slot
  pulsé). « Non identifiée » est acceptable (le premier passage la nommera) ; un mauvais nom ne l'est pas.
- [ ] Une voiture signalée comme immobile au plafond : vérifie qu'elle est sur son slot et sur le rail, sans manette
  tenue.

## 7. Lancer avec 1 voiture, puis 2, et surveiller l'identité

- [ ] **Une voiture** : trackloc **Lancer le suivi**, slot 1, champ latence à 0 la première fois. La page ne fait
  qu'une action à la fois : elle ne peut pas enregistrer pendant un suivi. Pour la vérification de détection
  ci-dessous, enregistre le même suivi en ligne de commande
  (`python -m trackloc run --camera iphone --map track_map.json --slots 0 --record rec1`). Conduis-la à la main 5
  tours, ou laisse un bot à gaz constant 4 la conduire. Dans RaceLab la voiture suit sa vraie position sur la carte.
  Après le suivi, trackloc affiche la **latence encore manquante** : note-la ; si elle dépasse 100 ms, clique
  **Utiliser** et cartographie à nouveau (la ligne d'arrivée en dépend).
- [ ] **Détection** : sur cet enregistrement, `python -m trackloc replay <dossier> --map track_map.json --debug
  overlay.mp4 --slots 0`. **GO si la voiture est détectée dans plus de 95 % des images où elle est visible** (hors zones
  aveugles). Il n'y a pas de compteur intégré : parcours la vidéo annotée, prends 200 images consécutives hors zones
  aveugles et compte celles où la détection est sur la voiture. Note le chiffre.
- [ ] **Deux voitures**, une par voie, puis dans la même voie, 5 tours chacune, avec l'appel d'abord.
- [ ] **Identité** : le nom affiché sur chaque voiture doit rester celui de la voiture que tu connais. **GO : 0
  échange d'identité sur 20 tours** au total avec 2 voitures ou plus (un échange = deux voitures s'échangent les noms,
  même si le passage suivant le corrige). `replay` affiche une ligne `identity:` : `relabels` compte chaque correction
  à une ligne d'arrivée, y compris le premier nommage de chaque voiture ; regarde de près tout relabel au-delà d'un
  par voiture.
- [ ] Puis 3 et 4 voitures, seulement si les suivis à 2 voitures ont eu 0 échange.

## 8. Le pilote automatique en virage, en commençant par un plafond bas

Prérequis : la carte est dans RaceLab avec une longueur en mètres (étape 5), les positions arrivent (étape 7), le rendu
et l'arrêt d'urgence ont réussi. Ouvre les Réglages de la voiture Auto (l'engrenage à côté de son interrupteur) et
choisis **Vitesse en virage (carte)**.

- [ ] **Règle le plafond avant que la voiture bouge : 4** (le défaut est 12, trop haut pour un premier essai).
  Adhérence au départ 10 m/s^2 et marge 5 % sont les défauts ; pour le premier vrai essai, prends une marge de 15 %.
  Le `max_throttle` global s'applique en plus.
- [ ] Une voiture seule sur la piste. Passe-la en Auto, regarde 3 tours une main sur STOP. RÉUSSI : elle fait les
  tours, ralentit avant les virages, aucune sortie de piste.
- [ ] Monte le plafond d'un cran seulement après 3 tours propres au plafond actuel. Arrête à la première sortie de
  piste, ou quand elle est nettement plus lente que ton propre tour.
- [ ] Test du repli, une fois, au plafond 4 : arrête le **Lancer** de trackloc pendant que la voiture est en Auto.
  Après 0,5 s sans positions, le profil tient le temps au tour visé : ni arrêt brutal ni accélération brutale. Relance.
- [ ] **Temps au tour** : note les 10 premiers tours. **GO : ils baissent** (meilleur des 5 derniers plus rapide que
  le meilleur des 5 premiers, sans nouvelle sortie de piste). Sinon, voir le dépannage.
- [ ] Les facteurs appris par virage sont remis à zéro à la séance suivante, après un STOP, et quand le profil est
  enregistré à nouveau : n'enregistre pas le profil entre deux tours si tu veux voir l'apprentissage.

## 9. Trois captures de calibration

Elles donnent les vrais chiffres du moteur ; le modèle du simulateur n'est qu'une estimation. Lance l'**Enregistrement**
de trackloc pour chacune (il enregistre les événements RaceLab, donc chaque changement de gaz est sur l'horloge de la
vidéo). Piste dégagée, une voiture.

- [ ] **A. Paliers de gaz 0 vers k sur une ligne droite** (dossier `A-steps`) : voiture à l'arrêt juste avant une droite,
  caméra dessus. Impulsions du Test matériel à k = 2, 3, 4, 5, 6 (1,5 s chacune, quelques secondes entre). Pour k = 8
  et 10, un bot à gaz constant avec le plafond à k, lancé sur la droite, arrêté par STOP à la fin de la droite suivante :
  note k et l'heure de départ.
- [ ] **B. Lâcher de gaz à deux vitesses** (dossiers `B-lift-lo`, `B-lift-hi`) : bot à gaz constant vers 4, puis vers 7,
  assez longtemps pour que la vitesse se stabilise (3 tours). Sur une droite, mets le gaz à 0 depuis RaceLab (le
  curseur Manuel à 0, pas STOP) et note l'heure. Trois lâchers par vitesse.
- [ ] **C. Délai d'actionnement** (dossier `C-delay`) : à l'arrêt, une impulsion du Test matériel à 4 pendant 1 s, 10
  fois, en enregistrant à la cadence la plus haute que l'iPhone donne (60 images/s si possible). Le délai est le temps
  entre l'événement de gaz dans `racelab.jsonl` et la première image où la voiture bouge. Note les 10 valeurs.

Pour chacune : la voiture, le niveau de batterie ou d'alimentation, et si la voiture était froide (premier essai) ou
chaude.

## 10. Quoi sauvegarder et renvoyer

- [ ] `~/.racelab/hardware-tests/hwtest-<date>.json` (le rapport du Test matériel avec tes notes).
- [ ] `~/.racelab/trackloc/serve/<date>-map/` et chaque dossier `<date>-record/`, A, B et C compris. Les vidéos sont
  lourdes : copie-les sur un disque et envoie d'abord les fichiers JSON.
- [ ] Le journal de RaceLab (Système → Journal, copié dans un fichier texte), la version de la CU (Système →
  Connexion), le transport.
- [ ] Les chiffres : latence BLE p95 / médiane / max / p95 d'acquittement de `C` ; longueur de carte contre mètre
  ruban ; taille du marqueur mesurée ; % de détection ; échanges d'identité sur N tours ; durée du rendu (deux
  mesures) ; résolution et cadence de la caméra ; l'estimation de latence ; temps au tour de l'étape 8 et plafond atteint.
- [ ] Des photos : le support de la caméra, le marqueur, la piste vue par la caméra.

## Tableau go / no-go

| Critère | GO | Sinon |
|---|---|---|
| Sur cales : arrêt d'urgence, homme mort, coupure Bluetooth | la voiture s'arrête ou revient sur sa manette en environ 1 s, à chaque fois | pas de test sur piste ; envoie le journal |
| Lien du Test matériel | transport `C`, bots pris en charge | stop : `C` n'est pas ce qui est testé |
| Latence BLE | rien de perdu, p95 <= 150 ms, aucun aller-retour > 400 ms ; **p95 noté** | rapproche-toi ; deux échecs : pas de bots en BLE |
| Rendu après perte de lien | la voiture s'arrête environ 750 ms après l'arrêt du rafraîchissement (0,5 à 1,0 s), deux fois | ne conduis pas de bots sur cette CU |
| Visée | « marqueur OK », couverture OK, exposition verrouillée | corrige le support ou la lumière avant de cartographier |
| Longueur de la carte | à moins de 2 % du mètre ruban | ne lance pas ; vérifie la taille du marqueur ; recommence |
| Détection | plus de 95 % des images où la voiture est visible | plus de lumière, exposition verrouillée, fond piste vide ; envoie l'enregistrement |
| Identité | 0 échange sur 20 tours | envoie l'enregistrement ; pas d'autopilote à plusieurs voitures |
| Pilote automatique | 3 tours propres à chaque plafond, temps au tour en baisse | retour au Rythme cible ; envoie l'enregistrement |

## Dépannage

| Symptôme | Cause probable | Que faire |
|---|---|---|
| **La caméra ne s'ouvre pas** | le terminal n'a pas la permission caméra ; l'iPhone est verrouillé, loin ou sur un autre identifiant Apple ; une autre appli tient la caméra | Réglages Système → Confidentialité et sécurité → Caméra : autorise ton terminal et relance-le. Déverrouille l'iPhone, rapproche-le, Wi-Fi et Bluetooth activés. Ferme Zoom, Photo Booth, FaceTime. `python -m trackloc probe`. Essaie `--ffmpeg` ou une URL de flux (une appli de caméra IP). Dans `serve`, choisis-la dans la liste et clique à nouveau *Ouvrir la caméra* |
| **Marqueur introuvable** | trop petit ou trop loin ; imprimé à l'échelle (« ajuster à la page ») ; reflet sur papier brillant ; pas à plat ; en partie hors du champ | le carré doit faire au moins environ 50 px sur l'image. Réimprime à 100 % sur papier mat, colle à plat, pose-le sur la table dans le champ, supprime le reflet, verrouille l'exposition |
| **La voiture ne bouge pas sur une impulsion** | gaz trop bas pour cette voiture ; une manette est tenue sur ce slot ; voiture hors du rail ou sur un point mort ; bots coupés ; l'étape dit Non lancé | lis la ligne sous la pastille. Monte à 4, 5, 6. Lâche la manette. Vérifie la voiture sur son slot, l'alimentation et l'interrupteur *Avec bots*. Si le panneau dit que le lien n'a jamais confirmé, lis la porte (gate) dans le message et Système → Journal |
| **RaceLab refuse (403, « host not allowed »)** | RaceLab est joint par un nom qui n'est ni `localhost` ni une IP, ou depuis une autre origine | utilise `http://127.0.0.1:8130/` ou `localhost`. Pour un autre nom, ajoute-le à `[api] allowed_hosts` dans `racelab.toml` (`allowed_origins` pour une page d'une autre origine). Un jeton défini dans `[api]` doit être donné à trackloc avec `--token`. Pour la page de trackloc elle-même : 421 = mauvais Host, 403 = clé `?k=` absente ou requête inter-sites : utilise l'adresse affichée par ce lancement |
| **trackloc répond 409** | une autre action tourne | appuie sur STOP dans la page trackloc et attends que l'état repasse au repos |
| **Pas de pastille de connecteur dans RaceLab** | RaceLab démarré après `serve` (il réessaie), ou version plus ancienne sans `connector.set` | attends quelques secondes ; mets RaceLab à jour ; trackloc affiche un seul avertissement dans son terminal |
| **Les temps au tour ne s'améliorent pas** | pas de positions fraîches (le profil est retombé sur le temps au tour visé) ; la forme n'a pas de longueur en mètres ; un virage gelé après une sortie de piste ; plafond trop bas ; voiture froide ou batterie faible ; latence de la caméra non corrigée | vérifie que le panneau Piste montre des positions qui bougent et une longueur ; si la voiture lève là où il ne faut pas, monte l'adhérence au départ, si elle sort, baisse-la ou élargis la marge ; monte le plafond d'un cran ; cartographie à nouveau avec l'estimation de latence ; fais chauffer la voiture |
| **Les voitures s'échangent les noms** | voitures groupées, une main au-dessus de la piste, une zone aveugle, mêmes couleurs | enregistre et envoie ; appel avec les voitures à une main d'écart ; vérifie la latence de la caméra |
| **Longueur de carte loin du ruban** | mauvaise taille de marqueur ; marqueur pas à plat ou pas dans le plan de la piste ; distorsion de l'objectif | mesure à nouveau le carré ; repose le marqueur ; utilise l'objectif principal de l'iPhone, pas l'ultra grand-angle ; envoie l'enregistrement |

## Non vérifié

Tout ceci n'a tourné que sur la CU virtuelle et des images de synthèse. Les limites de ce fichier sont soit imposées par
le panneau Test matériel (150 ms, 400 ms, gaz 6), soit le jugement de ce plan (0,5 à 1,0 s, 2 %, 95 %, 20 tours,
plafond 4, marge 15 %). Le vrai circuit peut en demander d'autres : enregistre ce que tu vois et ne plie pas les
données aux limites.
