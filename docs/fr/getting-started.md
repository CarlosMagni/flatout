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

## 5. Quitter

Système → **« Quitter RaceLab »** : les bots sont coupés et rendus aux manettes.
Fermer la fenêtre ne suffit pas : RaceLab continue de tourner en arrière-plan.

## Avant de faire rouler des bots sur la vraie piste

Les pilotes logiciels demandent une CU avec un firmware modifié (commande `B`,
voir [bot-protocol.md](../bot-protocol.md), en anglais) ; ce firmware n'est
**pas** fourni ici. Après tout flash, faire un essai au banc voiture soulevée
(gaz 3, 6, 0 ; manette ignorée pendant le bot ; un seul changement de voie par
appui ; STOP qui coupe) avant de laisser rouler un bot. Sans ce firmware,
RaceLab fonctionne en mode classique.

## En cas de souci

- « Bluetooth indisponible » : Réglages Système → Confidentialité et sécurité →
  Bluetooth → autoriser RaceLab, et vérifier que le Bluetooth est allumé.
- Journal : Système → Journal, ou le fichier `~/.racelab/racelab.log`.
- Réinstaller : relancer l'installeur (sans risque, il ne touche pas aux séances
  ni aux réglages).

Plus de détails dans le [guide utilisateur](../user-guide.md) (en anglais).
