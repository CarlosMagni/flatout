# Démarrer RaceLab

English: [docs/getting-started.md](../getting-started.md)

## 1. Installer (une seule fois)

- **macOS** : double-clic sur **`Install RaceLab.command`** (fabrique `RaceLab.app`).
- **Windows** : double-clic sur **`Install RaceLab.bat`** (raccourcis Bureau et menu Démarrer).
- **Linux** : `python3 install.py` (entrée « RaceLab » dans le menu des applications).

Il faut Python 3.11 ou plus récent et une connexion Internet (1 à 2 minutes).

Si macOS refuse d'ouvrir le fichier : clic droit → Ouvrir → Ouvrir. Sous Windows,
« Windows a protégé votre ordinateur » : Informations complémentaires → Exécuter quand même.

## 2. Lancer

Double-clic sur **`RaceLab.app`** (macOS) ou sur le raccourci **RaceLab** (Windows,
Linux). RaceLab s'ouvre dans sa propre fenêtre si Chrome, Brave ou Edge est
installé, sinon dans le navigateur.

Relancer RaceLab quand il tourne déjà rouvre simplement la fenêtre.

## 3. Premier lancement : choisir la CU

- **AppConnect Bluetooth** — CU allumée, AppConnect branché, app Carrera du
  téléphone fermée (l'AppConnect n'accepte qu'une connexion), puis
  « Se connecter ». macOS demande d'autoriser le Bluetooth pour RaceLab :
  accepter.
- **Câble série** — un adaptateur USB-série branché directement sur la CU ;
  choisir le port dans la liste. Sous Linux, s'ajouter d'abord au groupe
  `dialout` (`sudo usermod -aG dialout $USER`, puis se reconnecter).
- **CU virtuelle** — pour essayer sans matériel.

RaceLab s'en souvient. Pour changer : Système → Connexion.

## 4. Rouler

Séance → choisir le type → « Mettre en grille » → Course → « Départ »
(ou la barre Espace). STOP (ou Échap) coupe tous les bots.

## 5. Jouer contre l'ordinateur (bots)

Écran Course → **Activer les bots** (ou **Avec bots** en haut). Allume les
voitures que l'ordinateur doit piloter : en **Auto**, un bot attend le départ
puis roule tout seul. Les autres voitures restent à leurs manettes. **Revenir
au normal** rend toutes les voitures aux manettes.

Rien à installer sur la CU : les bots marchent avec la CU telle quelle
(firmware V339, la version actuelle).

## 6. Quitter

Système → **« Quitter RaceLab »** : les bots sont coupés et rendus aux manettes.
Fermer la fenêtre ne suffit pas : RaceLab continue de tourner en arrière-plan.

## Avant de faire rouler des bots sur la vraie piste

La première fois, faire un essai au banc voiture soulevée : une voiture allumée
en **Manuel**, curseur à 3, 6, 0 ; manette ignorée pendant le bot ; un seul
changement de voie par appui ; STOP qui coupe. Détails dans le
[guide utilisateur](../user-guide.md#before-the-first-real-test-the-bench) (en anglais).

Sur la vraie piste, Système → **Test matériel** valide pas à pas la commande C de la CU
(lien, latence, impulsion de gaz, changement de voie, rendu au bout de 0,75 s) et produit
un rapport JSON à partager. Pose la voiture sur une ligne droite et garde une main sur
STOP. Détails dans le [guide utilisateur](../user-guide.md#hardware-test-system-tab) (en anglais).

Si l'app dit que les bots sont indisponibles, la CU est plus ancienne que la
V338 : mets-la à jour avec la V339 d'origine dans Système → Options avancées
(voir le [guide utilisateur](../user-guide.md#updating-the-cu-firmware), en anglais).

## En cas de souci

- « Bluetooth indisponible » : Réglages Système → Confidentialité et sécurité →
  Bluetooth → autoriser RaceLab, et vérifier que le Bluetooth est allumé.
- Journal : Système → Journal, ou le fichier `~/.racelab/racelab.log`.
- Réinstaller : relancer l'installeur (sans risque, il ne touche pas aux séances
  ni aux réglages).

Plus de détails dans le [guide utilisateur](../user-guide.md) (en anglais).

## Localisation par caméra (optionnel)

Le dossier [`trackloc/`](../../trackloc/README.md) fournit un connecteur caméra : un iPhone
(Continuity Camera) ou une webcam au-dessus de la piste, qui publie la forme du circuit et la
position des voitures dans RaceLab.

```bash
.venv/bin/pip install -e trackloc
source .venv/bin/activate      # dans chaque nouveau terminal
python -m trackloc marker      # repère à imprimer à 100 %, puis mesurer le carré noir
python -m trackloc probe       # caméras disponibles (autoriser l'accès caméra au terminal)
python -m trackloc map --camera iphone --slot 0 --marker-size 0.150   # tour de découverte automatique
python -m trackloc run --camera iphone --map track_map.json --slots 0,1
```

`--camera` accepte un numéro, une partie du nom (`iphone`), une URL de flux (`rtsp://…`), ou rien
(la commande liste les caméras et demande laquelle).

Le détail (enregistrement, latence, limites) est dans [`trackloc/README.md`](../../trackloc/README.md) (en anglais).
