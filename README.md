# Seerr Cleaner

**Français** · [English](README.en.md)

Interface web locale pour nettoyer les **médias fantômes** dans Seerr.

> **Compatibilité** — Conçu pour [Seerr](https://docs.seerr.dev/), le successeur unifié d'Overseerr et Jellyseerr. Fonctionne aussi avec les instances **Jellyseerr** encore en place : l'API `/api/v1` est identique. Nécessite une bibliothèque **Jellyfin** — les instances configurées avec Plex ne sont pas supportées.

## Le problème

Quand tu supprimes un film ou une série dans Radarr/Sonarr, **Seerr garde l'entrée en base**. Le contenu continue d'apparaître comme « Demandé » ou « Disponible » dans l'interface, alors que le fichier n'existe plus nulle part.

Ce n'est pas bloquant — les gens peuvent toujours redemander le contenu — mais au bout de quelques mois de ménage, ta bibliothèque se retrouve pleine d'entrées mortes. C'est sale.

Seerr a bien un bouton « Clear Media Data » sur la fiche de chaque média, mais il faut le faire **un par un**. Sur des centaines d'entrées, c'est inutilisable.

## La solution

Ce script compare ta base Seerr avec Radarr, Sonarr et Jellyfin, identifie les entrées orphelines, et te les présente dans une interface web avec les affiches. Tu coches, tu supprimes.

![Interface de nettoyage](accueil.jpg)

*L'onglet Historique, pour consulter les nettoyages passés :*

![Historique](historique.jpg)

## Installation

```bash
git clone https://github.com/karkarofff/seerr-cleaner.git
cd seerr-cleaner
pip install requests
python seerr_cleaner.py
```

**Aucune configuration à faire dans le code.** Au premier lancement, une page de configuration s'ouvre dans ton navigateur : tu y colles tes URL et tes clés API, tu peux tester chaque connexion, puis tu enregistres. Tout est stocké dans un `config.json` local, à côté du script. Aux lancements suivants, le scan démarre directement.

Où trouver les clés API :

| Application | Chemin |
|---|---|
| Seerr | Paramètres → Général → Clé API |
| Radarr / Sonarr | Settings → General → API Key |
| Jellyfin | Tableau de bord → Avancé → Clés API → `+` |

Les URL sont **sans slash final**. Si tes applications sont derrière un reverse proxy avec un sous-chemin (`https://serveur.com/radarr`), inclus le sous-chemin.

## Mode terminal / cron

Scanner sans navigateur (ne supprime jamais rien) :

    python seerr_cleaner.py --scan-only          # rapport lisible
    python seerr_cleaner.py --scan-only --json   # sortie JSON

Codes retour : 0 = aucun fantôme, 1 = fantômes trouvés, 2 = erreur.
Pratique en cron pour être alerté quand des fantômes apparaissent.

## Docker

    docker build -t seerr-cleaner .
    docker run -d --name seerr-cleaner -p 8765:8765 -v ./data:/data seerr-cleaner

Interface sur http://IP-du-serveur:8765 — config et backups persistés dans `./data`.

## Comment un média est identifié comme fantôme

Un média est signalé **uniquement** s'il est :

- absent de **toutes** les instances Radarr/Sonarr déclarées, **ET**
- absent de **Jellyfin**

La double vérification est importante : Seerr marque comme disponible tout ce qui est présent dans Jellyfin, y compris le contenu importé à la main qui n'est jamais passé par Radarr/Sonarr. Une comparaison avec les seuls *arr le classerait à tort comme fantôme.

Les médias en statut `PENDING` (demande en attente d'approbation), `UNKNOWN` et `BLACKLISTED` sont toujours ignorés.

## Instances 4K — à lire

Si tu fais tourner des **instances Radarr/Sonarr séparées pour le 4K**, déclare-les toutes : la page de configuration permet d'ajouter autant d'instances Radarr et Sonarr que nécessaire.

Si tu oublies une instance, **tout le contenu qui n'existe que dans cette instance sera vu comme fantôme** et proposé à la suppression. C'est le seul vrai moyen de te tirer une balle dans le pied avec cet outil.

Un avertissement s'affiche dans l'interface si plus de 40 % de ta base est proposée à la suppression — c'est en général le signe qu'une instance manque à l'appel.

## Sécurité

C'est un outil qui supprime des choses, alors autant être clair sur ce qu'il fait :

- **Tout tourne en local.** Le serveur n'écoute que sur `127.0.0.1`. Tes clés API ne quittent jamais ta machine : elles sont stockées dans `config.json`, sur ton disque.
- **Radarr, Sonarr et Jellyfin ne sont jamais modifiés.** Le script fait uniquement des `GET` dessus. Les endpoints de suppression de ces applications ne sont même pas implémentés dans le code.
- **Aucun fichier vidéo n'est jamais supprimé.** Le script n'a aucun accès au système de fichiers de ton serveur.
- Le seul appel destructif est `DELETE /api/v1/media/{id}` sur Seerr — exactement ce que fait le bouton « Clear Media Data » de l'interface.
- **Un backup JSON est écrit avant chaque suppression** (dossier `backups/`), avec les identifiants Seerr, TMDB/TVDB, titres et statuts.
- Le scan **s'interrompt** si une source renvoie une bibliothèque vide. Sans ce garde-fou, un Radarr temporairement hors ligne ferait passer toute ta bibliothèque pour fantôme.

Pire scénario réaliste : tu perds l'historique de demande d'un média dans Seerr. Jamais le fichier.

> ⚠ Le fichier `config.json` contient tes clés API en clair. Il est listé dans le `.gitignore` fourni — ne le commit jamais.

## Utilisation

Le navigateur s'ouvre sur `http://127.0.0.1:8765`. Le scan prend de quelques secondes à une ou deux minutes selon la taille de ta bibliothèque.

Ensuite :

- Clique sur une affiche pour la sélectionner (elle passe en rouge)
- Filtre par titre, par type (film/série) ou par **statut**
- Trie par titre, statut, année ou type
- « Tout cocher » ne coche que ce qui est **visible après filtrage**
- « Reconfigurer » te ramène à la page de configuration

### Historique

Un onglet « Historique » liste tous tes nettoyages passés (chaque suppression écrit un backup). Clique sur un nettoyage pour voir en détail les médias supprimés, avec affiches et statuts.

Chaque entrée a un bouton « Redemander » qui recrée une demande dans Seerr via l'API (`POST /api/v1/request`). **Ça ne restaure pas le fichier vidéo** : ça agit comme si tu cliquais « Demander » dans Seerr. C'est utile uniquement si le média existe encore quelque part et que tu as effacé l'entrée par erreur — pour un vrai fantôme, le redemander déclencherait juste un nouveau téléchargement.

Les statuts sont colorés :

| Statut | Signification |
|---|---|
| `DELETED` | Seerr sait déjà que le média a été supprimé. Le plus sûr à nettoyer. |
| `PROCESSING` | Seerr croit le téléchargement en cours. À vérifier avant de supprimer. |
| `AVAILABLE` | Seerr le croit disponible. Vérifie dans Jellyfin avant de supprimer. |

**Approche recommandée** : filtre sur `DELETED`, coche tout, supprime. Tu liquides le gros du lot sans risque. Traite le reste au cas par cas.

## Cas particuliers

**Un média en `AVAILABLE` apparaît comme fantôme alors qu'il est bien dans Jellyfin.** Il n'a probablement pas d'identifiant TMDB/TVDB dans Jellyfin (métadonnées mal identifiées). Le script ne peut pas faire le lien. Corrige les métadonnées dans Jellyfin (Identifier → recherche TMDB) plutôt que de supprimer l'entrée.

**Titre affiché « fiche TMDB introuvable ».** La fiche a été supprimée ou fusionnée côté TMDB. L'entrée Seerr est une coquille vide, tu peux la nettoyer sans crainte.

## Ce qui n'est pas supporté

- **Plex.** Seerr gère Plex, Jellyfin et Emby, mais ce script vérifie la bibliothèque via l'API Jellyfin. Une instance Seerr configurée avec Plex nécessiterait de réécrire cette partie. Contributions bienvenues.
- Nettoyage au niveau saison. Si une série existe encore dans Sonarr, elle est conservée entièrement, même si tu as supprimé des saisons.

## Licence

MIT. Fourni sans aucune garantie. Vérifie ce que tu supprimes.
