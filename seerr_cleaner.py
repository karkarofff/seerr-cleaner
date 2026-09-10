#!/usr/bin/env python3
"""
seerr-cleaner — Nettoyage des medias fantomes dans Seerr.

COMPATIBILITE
Concu pour Seerr (le successeur unifie d'Overseerr et Jellyseerr, sorti en
2026). Fonctionne aussi avec les instances Jellyseerr encore en place :
l'API /api/v1 est identique. Necessite une bibliotheque Jellyfin — les
instances Seerr configurees avec Plex ne sont pas supportees.

PROBLEME RESOLU
Quand on supprime un film ou une serie dans Radarr/Sonarr, Seerr garde
l'entree en base. Le contenu continue d'apparaitre comme "Demande" ou
"Disponible" dans l'interface. Ce script detecte ces entrees fantomes et
permet de les effacer (equivalent du bouton "Clear Media Data" de Seerr,
mais en masse, avec une interface visuelle).

FONCTIONNEMENT
Un media est considere comme fantome s'il est absent de TOUTES les instances
Radarr/Sonarr declarees ET absent de Jellyfin. La double verification evite
de supprimer du contenu importe manuellement dans Jellyfin.

TOUT TOURNE EN LOCAL
Au premier lancement, une page de configuration s'ouvre dans le navigateur.
Tu y entres tes URL et cles API. Elles sont enregistrees dans un fichier
config.json a cote du script, SUR TA MACHINE. Rien n'est jamais envoye
ailleurs : le serveur n'ecoute que sur 127.0.0.1.

SECURITE
- Radarr / Sonarr / Jellyfin : lecture seule (GET). Jamais modifies.
- Aucun fichier video n'est jamais supprime.
- Seul appel destructif : DELETE /api/v1/media/{id} sur Seerr.
- Un backup JSON est ecrit avant chaque suppression.
- config.json reste en local. NE PAS le commit (voir .gitignore).
- Le scan s'interrompt si une source renvoie une bibliotheque vide.

Licence MIT. Fourni sans garantie : verifie ce que tu supprimes.

Usage :
    pip install requests
    python seerr_cleaner.py
"""

import json
import os
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# =====================================================================
#  PARAMETRES GENERAUX (la config utilisateur se fait via le navigateur)
# =====================================================================

PORT = 8765
# 0.0.0.0 pour Docker / acces distant : SEERR_CLEANER_HOST=0.0.0.0
BIND_HOST = os.environ.get("SEERR_CLEANER_HOST", "127.0.0.1")
TIMEOUT = 30
WORKERS = 6
CONFIG_FILE = "config.json"
# Dossier des donnees (config + backups). Par defaut : a cote du script.
# Docker / autre emplacement : SEERR_CLEANER_DATA=/data
DATA_DIR = (os.environ.get("SEERR_CLEANER_DATA")
            or os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

# =====================================================================

STATUS = {1: "UNKNOWN", 2: "PENDING", 3: "PROCESSING",
          4: "PARTIALLY_AVAILABLE", 5: "AVAILABLE", 6: "BLACKLISTED",
          7: "DELETED"}

IGNORED_STATUS = {1, 2, 6}
TMDB_IMG = "https://image.tmdb.org/t/p/w300"

CONFIG = {}


class ScanError(Exception):
    pass


# ---------------------------------------------------------------------
#  CONFIG (lecture / ecriture du config.json local)
# ---------------------------------------------------------------------

def config_path():
    return os.path.join(DATA_DIR, CONFIG_FILE)


def load_config():
    global CONFIG
    try:
        with open(config_path(), encoding="utf-8") as f:
            CONFIG = json.load(f)
        return True
    except (FileNotFoundError, ValueError):
        CONFIG = {}
        return False


def save_config(cfg):
    global CONFIG
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    CONFIG = cfg


def config_is_complete():
    if not CONFIG:
        return False
    if not CONFIG.get("seerr", {}).get("url") or not CONFIG.get("seerr", {}).get("key"):
        return False
    if not CONFIG.get("jellyfin", {}).get("url") or not CONFIG.get("jellyfin", {}).get("key"):
        return False
    if not CONFIG.get("radarr"):
        return False
    if not CONFIG.get("sonarr"):
        return False
    return True


# ---------------------------------------------------------------------
#  Helpers HTTP
# ---------------------------------------------------------------------

def get_json(url, path, headers, params=None, label=""):
    try:
        r = requests.get(f"{url}{path}", headers=headers,
                         params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise ScanError(f"[{label}] Connexion impossible : {e}")
    if r.status_code == 401:
        raise ScanError(f"[{label}] HTTP 401 — cle API invalide.")
    if r.status_code != 200:
        raise ScanError(f"[{label}] HTTP {r.status_code} sur {path}. "
                        f"Verifie l'URL de base et la cle API.")
    try:
        return r.json()
    except ValueError:
        raise ScanError(f"[{label}] Reponse non-JSON (page HTML ?). "
                        f"L'URL de base est probablement fausse.")


def test_endpoint(kind, url, key):
    """Teste une connexion unique. Renvoie (ok, message)."""
    url = url.rstrip("/")
    try:
        if kind == "seerr":
            get_json(url, "/api/v1/status", {"X-Api-Key": key}, label="Seerr")
            return True, "Seerr OK"
        if kind == "radarr":
            d = get_json(url, "/api/v3/movie", {"X-Api-Key": key}, label="Radarr")
            return True, f"Radarr OK — {len(d)} films"
        if kind == "sonarr":
            d = get_json(url, "/api/v3/series", {"X-Api-Key": key}, label="Sonarr")
            return True, f"Sonarr OK — {len(d)} series"
        if kind == "jellyfin":
            d = get_json(url, "/Users", {"X-Emby-Token": key}, label="Jellyfin")
            return True, f"Jellyfin OK — {len(d)} utilisateur(s)"
    except ScanError as e:
        return False, str(e)
    return False, "Type inconnu"


# ---------------------------------------------------------------------
#  COLLECTE
# ---------------------------------------------------------------------

def fetch_arr(instances, endpoint, id_field, label):
    if not instances:
        raise ScanError(f"Aucune instance {label} declaree.")
    ids = set()
    for inst in instances:
        url = inst["url"].rstrip("/")
        d = get_json(url, endpoint, {"X-Api-Key": inst["key"]},
                     label=f"{label} {url}")
        found = {x[id_field] for x in d if x.get(id_field)}
        if not found:
            raise ScanError(
                f"L'instance {label} {url} renvoie une bibliotheque VIDE. "
                f"Arret : sans elle, son contenu serait considere comme fantome. "
                f"Si cette instance est reellement vide, retire-la de la config.")
        ids |= found
    return ids


def fetch_jellyfin():
    url = CONFIG["jellyfin"]["url"].rstrip("/")
    h = {"X-Emby-Token": CONFIG["jellyfin"]["key"]}
    users = get_json(url, "/Users", h, label="Jellyfin")
    if not users:
        raise ScanError("Jellyfin : aucun utilisateur trouve. Cle API invalide ?")
    admin = next((u for u in users
                  if u.get("Policy", {}).get("IsAdministrator")), users[0])

    d = get_json(url, "/Items", h, params={
        "userId": admin["Id"], "recursive": "true",
        "includeItemTypes": "Movie,Series", "fields": "ProviderIds",
    }, label="Jellyfin")

    items = d.get("Items", [])
    if not items:
        raise ScanError("Jellyfin renvoie une bibliotheque VIDE. Arret : sans elle, "
                        "tout le contenu importe a la main passerait pour fantome.")
    tmdb, tvdb = set(), set()
    for it in items:
        for k, v in (it.get("ProviderIds") or {}).items():
            if not v:
                continue
            try:
                if k.lower() == "tmdb":
                    tmdb.add(int(v))
                elif k.lower() == "tvdb":
                    tvdb.add(int(v))
            except (TypeError, ValueError):
                pass
    return tmdb, tvdb, len(items)


def fetch_seerr_media():
    url = CONFIG["seerr"]["url"].rstrip("/")
    h = {"X-Api-Key": CONFIG["seerr"]["key"]}
    out, skip, take = [], 0, 100
    while True:
        d = get_json(url, "/api/v1/media", h,
                     params={"take": take, "skip": skip}, label="Seerr")
        res = d.get("results", [])
        out.extend(res)
        total = d.get("pageInfo", {}).get("results", len(out))
        skip += take
        if skip >= total or not res:
            break
    if not out:
        raise ScanError("Seerr ne renvoie aucun media.")
    return out


def fetch_details(mtype, tmdb_id):
    url = CONFIG["seerr"]["url"].rstrip("/")
    path = f"/api/v1/movie/{tmdb_id}" if mtype == "movie" else f"/api/v1/tv/{tmdb_id}"
    try:
        r = requests.get(f"{url}{path}",
                         headers={"X-Api-Key": CONFIG["seerr"]["key"]}, timeout=15)
        if r.status_code == 200:
            j = r.json()
            date = j.get("releaseDate") or j.get("firstAirDate") or ""
            return {
                "title": j.get("title") or j.get("name") or "(titre inconnu)",
                "year": date[:4] if date else "",
                "poster": TMDB_IMG + j["posterPath"] if j.get("posterPath") else None,
            }
    except requests.RequestException:
        pass
    return {"title": f"(fiche TMDB introuvable — id {tmdb_id})",
            "year": "", "poster": None}


def run_scan():
    radarr = fetch_arr(CONFIG["radarr"], "/api/v3/movie", "tmdbId", "Radarr")
    sonarr = fetch_arr(CONFIG["sonarr"], "/api/v3/series", "tvdbId", "Sonarr")
    jf_tmdb, jf_tvdb, jf_count = fetch_jellyfin()
    media = fetch_seerr_media()

    ghosts, kept = [], 0
    for m in media:
        mtype, status = m.get("mediaType"), m.get("status", 1)
        tmdb, tvdb = m.get("tmdbId"), m.get("tvdbId")
        if status in IGNORED_STATUS:
            kept += 1
            continue
        if mtype == "movie":
            if not tmdb or tmdb in radarr or tmdb in jf_tmdb:
                kept += 1
                continue
        elif mtype == "tv":
            # Jellyfin ne renseigne pas toujours l'id Tvdb sur les series
            # (selon les agents de metadonnees) : on verifie AUSSI par Tmdb
            # pour eviter de classer fantome une serie bien presente.
            if (not tvdb or tvdb in sonarr or tvdb in jf_tvdb
                    or (tmdb and tmdb in jf_tmdb)):
                kept += 1
                continue
        else:
            kept += 1
            continue
        ghosts.append({"id": m.get("id"), "type": mtype, "tmdbId": tmdb,
                       "tvdbId": tvdb, "status": STATUS.get(status, str(status))})

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        details = list(ex.map(lambda g: fetch_details(g["type"], g["tmdbId"]), ghosts))
    for g, d in zip(ghosts, details):
        g.update(d)

    return {
        "stats": {
            "radarr": len(radarr), "radarr_instances": len(CONFIG["radarr"]),
            "sonarr": len(sonarr), "sonarr_instances": len(CONFIG["sonarr"]),
            "jellyfin": jf_count, "seerr": len(media),
            "ghosts": len(ghosts), "kept": kept,
        },
        "ghosts": ghosts,
    }


def write_backup(entries):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR,
                        f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"deleted_at": datetime.now().isoformat(),
                   "seerr_url": CONFIG["seerr"]["url"],
                   "count": len(entries), "entries": entries},
                  f, indent=2, ensure_ascii=False)
    return path


def delete_media(entries):
    backup = write_backup(entries)
    print(f"  Backup ecrit : {backup}")
    url = CONFIG["seerr"]["url"].rstrip("/")
    h = {"X-Api-Key": CONFIG["seerr"]["key"]}
    res = []
    for e in entries:
        mid = e["id"]
        try:
            r = requests.delete(f"{url}/api/v1/media/{mid}", headers=h, timeout=TIMEOUT)
            res.append({"id": mid, "ok": r.status_code in (200, 204),
                        "code": r.status_code})
        except requests.RequestException as e2:
            res.append({"id": mid, "ok": False, "code": str(e2)})
        time.sleep(0.2)
    return {"results": res, "backup": backup}


# ---------------------------------------------------------------------
#  HISTORIQUE DES BACKUPS
# ---------------------------------------------------------------------

def list_backups():
    """Liste les backups du dossier, du plus recent au plus ancien."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for name in os.listdir(BACKUP_DIR):
        if not (name.startswith("backup_") and name.endswith(".json")):
            continue
        path = os.path.join(BACKUP_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            out.append({
                "file": name,
                "deleted_at": data.get("deleted_at", ""),
                "count": data.get("count", len(data.get("entries", []))),
            })
        except (ValueError, OSError):
            out.append({"file": name, "deleted_at": "", "count": 0,
                        "error": "fichier illisible"})
    out.sort(key=lambda b: b["file"], reverse=True)
    return out


def read_backup(name):
    """Contenu d'un backup. Le nom est valide strictement (anti-traversal)."""
    if not (name.startswith("backup_") and name.endswith(".json")) \
       or "/" in name or "\\" in name or ".." in name:
        raise ScanError("Nom de backup invalide.")
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(path):
        raise ScanError("Backup introuvable.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def rerequest_media(entry):
    """Recree une demande dans Seerr (POST /api/v1/request).
    NB : ne restaure PAS le fichier — recree seulement la demande, comme si
    un utilisateur cliquait 'Demander'. Utile uniquement si le media existe
    encore quelque part et qu'on a efface l'entree Seerr par erreur."""
    url = CONFIG["seerr"]["url"].rstrip("/")
    h = {"X-Api-Key": CONFIG["seerr"]["key"], "Content-Type": "application/json"}
    tmdb = entry.get("tmdbId")
    tvdb = entry.get("tvdbId")
    mtype = entry.get("type")
    if mtype == "movie":
        if not tmdb:
            return {"ok": False, "msg": "Pas de tmdbId"}
        payload = {"mediaType": "movie", "mediaId": tmdb}
    elif mtype == "tv":
        # Seerr identifie les series par tmdbId aussi ; on demande toutes les
        # saisons. Si seul le tvdb est connu, on tente une resolution.
        seer_tmdb = tmdb
        if not seer_tmdb and tvdb:
            return {"ok": False,
                    "msg": "Serie sans tmdbId — re-demande non disponible"}
        payload = {"mediaType": "tv", "mediaId": seer_tmdb, "seasons": "all"}
    else:
        return {"ok": False, "msg": f"Type non gere ({mtype})"}
    try:
        r = requests.post(f"{url}/api/v1/request", headers=h,
                          data=json.dumps(payload), timeout=TIMEOUT)
        if r.status_code in (200, 201):
            return {"ok": True, "msg": "Demande recreee"}
        return {"ok": False, "msg": f"HTTP {r.status_code}"}
    except requests.RequestException as e:
        return {"ok": False, "msg": str(e)}


# ---------------------------------------------------------------------
#  PAGE DE CONFIGURATION
# ---------------------------------------------------------------------

CONFIG_PAGE = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Seerr Cleaner — Configuration</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%234f8cff'/><g stroke='%23fff' stroke-width='2' stroke-linecap='round' fill='none'><path d='M21 7 L13 15'/><path d='M13 15 L10 18 L14 22 L17 19 Z' fill='%23fff' stroke='none'/><path d='M10 22 L8 26 M13 23 L12 27 M16 23 L16 27'/></g></svg>">
<style>
  :root { --bg:#12151c; --card:#1b1f2a; --line:#2a3040; --txt:#e6e9f0;
          --dim:#8b93a7; --acc:#4f8cff; --ok:#3fb950; --dang:#e5484d; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }
  .wrap { max-width:720px; margin:0 auto; padding:32px 24px 60px; }
  h1 { font-size:22px; margin:0 0 6px; }
  .sub { color:var(--dim); margin-bottom:28px; font-size:14px; }
  .sec { background:var(--card); border:1px solid var(--line); border-radius:12px;
         padding:20px; margin-bottom:18px; }
  .sec h2 { font-size:15px; margin:0 0 4px; }
  .sec .hint { color:var(--dim); font-size:12.5px; margin-bottom:14px; }
  label { display:block; font-size:12px; color:var(--dim); margin:10px 0 4px; }
  input { width:100%; background:#12151c; border:1px solid var(--line);
          color:var(--txt); padding:9px 11px; border-radius:8px; font-size:14px;
          font-family:inherit; }
  input:focus { outline:none; border-color:var(--acc); }
  .inst { border:1px solid var(--line); border-radius:8px; padding:12px;
          margin-bottom:10px; position:relative; }
  .inst .rm { position:absolute; top:8px; right:8px; background:none;
              border:none; color:var(--dim); cursor:pointer; font-size:18px; }
  .inst .rm:hover { color:var(--dang); }
  button { font-family:inherit; }
  .add { background:none; border:1px dashed var(--line); color:var(--dim);
         padding:8px 14px; border-radius:8px; cursor:pointer; font-size:13px; }
  .add:hover { border-color:var(--acc); color:var(--txt); }
  .test { background:var(--card); border:1px solid var(--line); color:var(--txt);
          padding:6px 12px; border-radius:7px; cursor:pointer; font-size:13px;
          margin-top:10px; }
  .test:hover { border-color:var(--acc); }
  .result { font-size:12.5px; margin-top:8px; padding:6px 10px; border-radius:6px;
            display:none; }
  .result.ok { display:block; background:#132a1a; color:var(--ok); }
  .result.ko { display:block; background:#2a1517; color:#f0a0a4; white-space:pre-wrap; }
  .save { background:var(--acc); border:none; color:#fff; font-weight:600;
          padding:12px 24px; border-radius:9px; cursor:pointer; font-size:15px;
          width:100%; margin-top:8px; }
  .save:disabled { opacity:.4; cursor:not-allowed; }
  .note { color:var(--dim); font-size:12px; margin-top:14px; text-align:center; }
  code { background:#12151c; padding:1px 5px; border-radius:4px; font-size:12px; }
</style></head><body>
<div class="wrap">
  <h1>Configuration</h1>
  <div class="sub">Ces informations sont enregistrees dans <code>config.json</code>,
    sur cette machine uniquement. Rien n'est envoye ailleurs.</div>

  <div class="sec">
    <h2>Seerr</h2>
    <div class="hint">Cle API : Parametres &rarr; General &rarr; Cle API. URL sans slash final.</div>
    <label>URL</label>
    <input id="seerr_url" placeholder="https://seerr.exemple.com">
    <label>Cle API</label>
    <input id="seerr_key" placeholder="cle API Seerr">
    <button class="test" data-kind="seerr">Tester la connexion</button>
    <div class="result" id="r_seerr"></div>
  </div>

  <div class="sec">
    <h2>Jellyfin</h2>
    <div class="hint">Cle API : Tableau de bord &rarr; Avance &rarr; Cles API &rarr; +</div>
    <label>URL</label>
    <input id="jf_url" placeholder="https://jellyfin.exemple.com">
    <label>Cle API</label>
    <input id="jf_key" placeholder="cle API Jellyfin">
    <button class="test" data-kind="jellyfin">Tester la connexion</button>
    <div class="result" id="r_jellyfin"></div>
  </div>

  <div class="sec">
    <h2>Radarr</h2>
    <div class="hint">Cle API : Settings &rarr; General &rarr; API Key.
      Si tu as une instance 4K separee, ajoute-la ici — sinon son contenu
      passera pour fantome.</div>
    <div id="radarr_list"></div>
    <button class="add" data-target="radarr">+ Ajouter une instance Radarr</button>
  </div>

  <div class="sec">
    <h2>Sonarr</h2>
    <div class="hint">Meme principe que Radarr.</div>
    <div id="sonarr_list"></div>
    <button class="add" data-target="sonarr">+ Ajouter une instance Sonarr</button>
  </div>

  <button class="save" id="save">Enregistrer et lancer le scan</button>
  <div class="note">Tu pourras revenir ici a tout moment via le bouton
    « Reconfigurer » de l'interface.</div>
</div>

<script>
const EXISTING = __CONFIG_JSON__;

function instBlock(target, url, key) {
  url = url || ''; key = key || '';
  const d = document.createElement('div');
  d.className = 'inst';
  d.innerHTML = `
    <button class="rm" title="Retirer">&times;</button>
    <label>URL</label>
    <input class="u" placeholder="https://${target}.exemple.com">
    <label>Cle API</label>
    <input class="k" placeholder="cle API ${target}">
    <button class="test" data-kind="${target}" data-local="1">Tester cette instance</button>
    <div class="result"></div>`;
  d.querySelector('.u').value = url;
  d.querySelector('.k').value = key;
  d.querySelector('.rm').onclick = () => d.remove();
  d.querySelector('.test').onclick = async () => {
    const box = d.querySelector('.result');
    box.className = 'result'; box.textContent = 'Test...'; box.style.display = 'block';
    const r = await fetch('/api/test', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kind: target, url: d.querySelector('.u').value,
                            key: d.querySelector('.k').value})
    }).then(r => r.json());
    box.className = 'result ' + (r.ok ? 'ok' : 'ko');
    box.textContent = r.message;
  };
  document.getElementById(target + '_list').appendChild(d);
}

if (EXISTING.seerr) {
  seerr_url.value = EXISTING.seerr.url || '';
  seerr_key.value = EXISTING.seerr.key || '';
}
if (EXISTING.jellyfin) {
  jf_url.value = EXISTING.jellyfin.url || '';
  jf_key.value = EXISTING.jellyfin.key || '';
}
(EXISTING.radarr && EXISTING.radarr.length ? EXISTING.radarr : [{}])
  .forEach(i => instBlock('radarr', i.url, i.key));
(EXISTING.sonarr && EXISTING.sonarr.length ? EXISTING.sonarr : [{}])
  .forEach(i => instBlock('sonarr', i.url, i.key));

document.querySelectorAll('.add').forEach(b =>
  b.onclick = () => instBlock(b.dataset.target));

document.querySelectorAll('.test:not([data-local])').forEach(b => b.onclick = async () => {
  const kind = b.dataset.kind;
  const url = (kind === 'seerr' ? seerr_url : jf_url).value;
  const key = (kind === 'seerr' ? seerr_key : jf_key).value;
  const box = document.getElementById('r_' + kind);
  box.className = 'result'; box.textContent = 'Test...'; box.style.display = 'block';
  const r = await fetch('/api/test', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({kind, url, key})
  }).then(r => r.json());
  box.className = 'result ' + (r.ok ? 'ok' : 'ko');
  box.textContent = r.message;
});

function collect(target) {
  return [...document.querySelectorAll('#' + target + '_list .inst')]
    .map(d => ({url: d.querySelector('.u').value.trim(),
                key: d.querySelector('.k').value.trim()}))
    .filter(i => i.url && i.key);
}

document.getElementById('save').onclick = async () => {
  const cfg = {
    seerr: {url: seerr_url.value.trim(), key: seerr_key.value.trim()},
    jellyfin: {url: jf_url.value.trim(), key: jf_key.value.trim()},
    radarr: collect('radarr'),
    sonarr: collect('sonarr'),
  };
  if (!cfg.seerr.url || !cfg.seerr.key || !cfg.jellyfin.url || !cfg.jellyfin.key
      || !cfg.radarr.length || !cfg.sonarr.length) {
    alert('Il manque des champs : Seerr, Jellyfin, et au moins une '
        + 'instance Radarr et une Sonarr sont requis.');
    return;
  }
  const btn = document.getElementById('save');
  btn.disabled = true; btn.textContent = 'Enregistrement...';
  await fetch('/api/save', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(cfg)
  });
  window.location.href = '/';
};
</script></body></html>"""

# ---------------------------------------------------------------------
#  PAGE PRINCIPALE
# ---------------------------------------------------------------------

MAIN_PAGE = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Seerr Cleaner</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%234f8cff'/><g stroke='%23fff' stroke-width='2' stroke-linecap='round' fill='none'><path d='M21 7 L13 15'/><path d='M13 15 L10 18 L14 22 L17 19 Z' fill='%23fff' stroke='none'/><path d='M10 22 L8 26 M13 23 L12 27 M16 23 L16 27'/></g></svg>">
<style>
  :root { --bg:#12151c; --card:#1b1f2a; --line:#2a3040; --txt:#e6e9f0;
          --dim:#8b93a7; --acc:#4f8cff; --dang:#e5484d; --warn:#e8a33d; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }
  header { position:sticky; top:0; z-index:10; background:rgba(18,21,28,.96);
           backdrop-filter:blur(8px); border-bottom:1px solid var(--line);
           padding:16px 24px; }
  h1 { margin:0 0 4px; font-size:18px; }
  .stats { color:var(--dim); font-size:13px; }
  .stats b { color:var(--txt); font-weight:600; }
  .bar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:14px; }
  button { background:var(--card); color:var(--txt); border:1px solid var(--line);
           padding:8px 14px; border-radius:8px; cursor:pointer; font-size:14px;
           font-family:inherit; }
  button:hover { border-color:var(--acc); }
  button.danger { background:var(--dang); border-color:var(--dang); color:#fff;
                  font-weight:600; margin-left:auto; }
  button.danger:disabled { opacity:.35; cursor:not-allowed; }
  #reconf { font-size:12.5px; color:var(--dim); padding:6px 10px; }
  input[type=search], select { background:var(--card); border:1px solid var(--line);
      color:var(--txt); padding:8px 12px; border-radius:8px; font-size:14px;
      font-family:inherit; }
  input[type=search] { min-width:200px; }
  .count { color:var(--dim); font-size:13px; }
  main { padding:24px; }
  .grid { display:grid; gap:16px;
          grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          overflow:hidden; cursor:pointer; position:relative; transition:.12s; }
  .card:hover { border-color:var(--acc); }
  .card.sel { border-color:var(--dang); box-shadow:0 0 0 1px var(--dang); }
  .card.sel .poster, .card.sel .noimg { opacity:.4; }
  .poster { width:100%; aspect-ratio:2/3; object-fit:cover; display:block;
            background:#232838; }
  .noimg { width:100%; aspect-ratio:2/3; display:flex; align-items:center;
           justify-content:center; color:var(--dim); font-size:12px;
           background:#232838; text-align:center; padding:10px; }
  .meta { padding:9px 10px; }
  .t { font-size:13px; font-weight:600; line-height:1.3;
       display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
       overflow:hidden; }
  .s { font-size:11px; color:var(--dim); margin-top:3px; }
  .badge { display:inline-block; padding:1px 6px; border-radius:4px;
           font-size:10px; font-weight:600; margin-top:5px; }
  .b-DELETED { background:#2a3040; color:#9aa3b5; }
  .b-PROCESSING { background:#4a3316; color:var(--warn); }
  .b-AVAILABLE { background:#1d3a26; color:#5fca7a; }
  .b-PARTIALLY_AVAILABLE { background:#1d3340; color:#5fa8ca; }
  .b-other { background:#3a2030; color:#d08bb0; }
  .tick { position:absolute; top:8px; right:8px; width:24px; height:24px;
          border-radius:6px; background:rgba(0,0,0,.6); border:1px solid #fff5;
          display:flex; align-items:center; justify-content:center;
          font-size:14px; color:transparent; }
  .card.sel .tick { background:var(--dang); border-color:var(--dang); color:#fff; }
  .msg { padding:60px 24px; text-align:center; color:var(--dim); }
  .warn { background:#3a2416; border:1px solid #7a4a1e; color:#f0c088;
          padding:12px 16px; border-radius:8px; margin-bottom:20px; font-size:14px; }
  .err { background:#3a1a1c; border:1px solid #7a2a2e; color:#f0a0a4;
         padding:16px; border-radius:8px; white-space:pre-wrap; }
  .tabs { display:flex; gap:4px; margin-bottom:10px; }
  .tab { background:none; border:none; color:var(--dim); padding:6px 14px;
         border-radius:7px; cursor:pointer; font-size:14px; font-family:inherit; }
  .tab:hover { color:var(--txt); }
  .tab.active { background:var(--card); color:var(--txt); border:1px solid var(--line); }
  .hist { display:grid; gap:10px; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); }
  .hcard { background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:14px 16px; cursor:pointer; transition:.12s; }
  .hcard:hover { border-color:var(--acc); }
  .hcard .d { font-size:15px; font-weight:600; }
  .hcard .c { font-size:12.5px; color:var(--dim); margin-top:3px; }
  .overlay { position:absolute; inset:0; background:rgba(0,0,0,.6); z-index:50;
             display:flex; align-items:flex-start; justify-content:center;
             padding:40px 20px; overflow-y:auto; }
  .modal { background:var(--bg); border:1px solid var(--line); border-radius:14px;
           width:100%; max-width:1000px; padding:22px; }
  .modal .mh { display:flex; align-items:center; margin-bottom:6px; }
  .modal .mh h2 { font-size:17px; margin:0; }
  .modal .mh .x { margin-left:auto; background:none; border:none; color:var(--dim);
                  font-size:24px; cursor:pointer; line-height:1; }
  .modal .mh .x:hover { color:var(--txt); }
  .modal .msub { color:var(--dim); font-size:13px; margin-bottom:18px; }
  .drow { display:flex; align-items:center; gap:12px; padding:10px 0;
          border-top:1px solid var(--line); }
  .drow img, .drow .np { width:44px; height:66px; border-radius:5px; object-fit:cover;
          background:#232838; flex-shrink:0; }
  .drow .np { display:flex; align-items:center; justify-content:center;
              color:var(--dim); font-size:9px; text-align:center; padding:3px; }
  .drow .info { flex:1; min-width:0; }
  .drow .info .dt { font-size:14px; font-weight:600; }
  .drow .info .dm { font-size:12px; color:var(--dim); margin-top:2px; }
  .drow .re { background:var(--card); border:1px solid var(--line); color:var(--txt);
              padding:7px 12px; border-radius:7px; cursor:pointer; font-size:13px;
              font-family:inherit; white-space:nowrap; }
  .drow .re:hover { border-color:var(--acc); }
  .drow .re:disabled { opacity:.5; cursor:default; }
  .re-note { background:#1d2733; border:1px solid #2f4356; color:#9cc0e0;
             font-size:12.5px; padding:10px 13px; border-radius:8px; margin-bottom:16px; }
</style></head><body>
<header>
  <h1>Seerr Cleaner</h1>
  <div class="tabs">
    <button class="tab active" id="tab_clean" onclick="showTab('clean')">Nettoyage</button>
    <button class="tab" id="tab_hist" onclick="showTab('hist')">Historique</button>
  </div>
  <div class="stats" id="stats">Scan en cours...</div>
  <div class="bar" id="bar" style="display:none">
    <input type="search" id="q" placeholder="Filtrer par titre...">
    <select id="ft"><option value="">Films + series</option>
      <option value="movie">Films</option><option value="tv">Series</option></select>
    <select id="fs"><option value="">Tous les statuts</option></select>
    <select id="sort"><option value="title">Tri : titre</option>
      <option value="status">Tri : statut</option>
      <option value="year">Tri : annee</option>
      <option value="type">Tri : type</option></select>
    <button id="all">Tout cocher</button>
    <button id="none">Tout decocher</button>
    <button id="reconf" onclick="location.href='/config'">Reconfigurer</button>
    <span class="count" id="count"></span>
    <button class="danger" id="go" disabled>Supprimer la selection</button>
  </div>
</header>
<main>
  <div id="view_clean">
    <div id="warn"></div>
    <div id="out" class="msg">Interrogation de Radarr, Sonarr, Jellyfin et Seerr...</div>
  </div>
  <div id="view_hist" style="display:none">
    <div id="hist_out" class="msg">Chargement de l'historique...</div>
  </div>
</main>
<div id="modal_root"></div>
<script>
let DATA = [], SEL = new Set();
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const KNOWN = ['DELETED','PROCESSING','AVAILABLE','PARTIALLY_AVAILABLE'];
const cls = s => 'b-' + (KNOWN.includes(s) ? s : 'other');

function visible() {
  const q = document.getElementById('q').value.toLowerCase();
  const t = document.getElementById('ft').value;
  const st = document.getElementById('fs').value;
  const sort = document.getElementById('sort').value;
  let list = DATA.filter(o => (!t || o.type === t) && (!st || o.status === st) &&
    (!q || o.title.toLowerCase().includes(q)));
  const byTitle = (a,b) => a.title.localeCompare(b.title, 'fr');
  if (sort === 'title')  list.sort(byTitle);
  if (sort === 'type')   list.sort((a,b) => a.type.localeCompare(b.type) || byTitle(a,b));
  if (sort === 'status') list.sort((a,b) => a.status.localeCompare(b.status) || byTitle(a,b));
  if (sort === 'year')   list.sort((a,b) => (b.year||'0').localeCompare(a.year||'0') || byTitle(a,b));
  return list;
}
function render() {
  const list = visible(), out = document.getElementById('out');
  document.getElementById('count').textContent =
    list.length === DATA.length ? `${DATA.length} affiche(s)` : `${list.length} / ${DATA.length} affiche(s)`;
  if (!list.length) {
    out.className = 'msg';
    out.textContent = DATA.length ? 'Aucun resultat pour ce filtre.' : 'Aucun media fantome. Ton Seerr est propre.';
    sync(); return;
  }
  out.className = 'grid';
  out.innerHTML = list.map(o => `
    <div class="card ${SEL.has(o.id)?'sel':''}" data-id="${o.id}">
      <div class="tick">&#10003;</div>
      ${o.poster ? `<img class="poster" loading="lazy" src="${esc(o.poster)}" alt="">`
                 : `<div class="noimg">${esc(o.title)}</div>`}
      <div class="meta"><div class="t">${esc(o.title)}</div>
        <div class="s">${o.type==='tv'?'Serie':'Film'}${o.year?' &middot; '+o.year:''}</div>
        <span class="badge ${cls(o.status)}">${esc(o.status)}</span></div>
    </div>`).join('');
  out.querySelectorAll('.card').forEach(c => c.onclick = () => {
    const id = +c.dataset.id;
    SEL.has(id) ? SEL.delete(id) : SEL.add(id);
    c.classList.toggle('sel'); sync();
  });
  sync();
}
function sync() {
  const btn = document.getElementById('go');
  btn.disabled = !SEL.size;
  btn.textContent = SEL.size ? `Supprimer ${SEL.size} entree(s)` : 'Supprimer la selection';
}
['q','ft','fs','sort'].forEach(id =>
  document.getElementById(id).addEventListener(id==='q'?'input':'change', render));
document.getElementById('all').onclick = () => { visible().forEach(o => SEL.add(o.id)); render(); };
document.getElementById('none').onclick = () => { SEL.clear(); render(); };
document.getElementById('go').onclick = async () => {
  const picked = [...SEL].map(i => DATA.find(o => o.id === i));
  const risky = picked.filter(o => o.status !== 'DELETED');
  let msg = `Effacer ${picked.length} entree(s) de Seerr ?\n\n` +
    picked.slice(0,12).map(o => `- ${o.title} [${o.status}]`).join('\n') +
    (picked.length > 12 ? `\n... et ${picked.length-12} autres` : '');
  if (risky.length)
    msg += `\n\n/!\\ ${risky.length} entree(s) ne sont PAS en statut DELETED.\nVerifie que c'est voulu.`;
  msg += `\n\nLes fichiers video et Radarr/Sonarr/Jellyfin ne sont PAS touches.\nUn backup JSON sera ecrit.`;
  if (!confirm(msg)) return;
  const btn = document.getElementById('go');
  btn.disabled = true; btn.textContent = 'Suppression...';
  const r = await fetch('/api/delete', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({entries: picked})
  }).then(r => r.json());
  const ok = r.results.filter(x => x.ok).length;
  alert(`${ok} entree(s) nettoyee(s).` + (ok < picked.length ? ` ${picked.length-ok} echec(s).` : '') + `\n\nBackup : ${r.backup}`);
  DATA = DATA.filter(o => !r.results.some(x => x.ok && x.id === o.id));
  SEL.clear();
  document.getElementById('stats').innerHTML = `<b>${DATA.length}</b> fantome(s) restant(s)`;
  render();
};
fetch('/api/scan').then(r => r.json()).then(d => {
  if (d.error) {
    document.getElementById('stats').textContent = 'Echec du scan';
    document.getElementById('out').className = '';
    document.getElementById('out').innerHTML = '<div class="err">' + esc(d.error) +
      '\n\nSi c\'est un probleme de configuration, clique sur Reconfigurer.</div>';
    document.getElementById('bar').style.display = 'flex';
    return;
  }
  DATA = d.ghosts; const s = d.stats;
  const ri = s.radarr_instances > 1 ? ` (${s.radarr_instances} inst.)` : '';
  const si = s.sonarr_instances > 1 ? ` (${s.sonarr_instances} inst.)` : '';
  document.getElementById('stats').innerHTML =
    `Radarr <b>${s.radarr}</b>${ri} &middot; Sonarr <b>${s.sonarr}</b>${si} &middot; ` +
    `Jellyfin <b>${s.jellyfin}</b> &middot; Seerr <b>${s.seerr}</b> &mdash; ` +
    `<b>${s.ghosts}</b> fantome(s), ${s.kept} conserve(s)`;
  document.getElementById('bar').style.display = 'flex';
  const counts = {};
  DATA.forEach(o => counts[o.status] = (counts[o.status]||0)+1);
  const fs = document.getElementById('fs');
  Object.entries(counts).sort((a,b)=>b[1]-a[1]).forEach(([k,v]) => {
    const o = document.createElement('option');
    o.value = k; o.textContent = `${k} (${v})`; fs.appendChild(o);
  });
  if (s.ghosts > s.seerr * 0.4)
    document.getElementById('warn').innerHTML =
      `<div class="warn">/!\\ ${Math.round(s.ghosts/s.seerr*100)}% de ta base est proposee ` +
      `a la suppression. Si tu as des instances Radarr/Sonarr 4K, verifie qu'elles sont ` +
      `declarees (bouton Reconfigurer) &mdash; sinon leur contenu apparait a tort comme fantome.</div>`;
  render();
});

/* ---- Onglets ---- */
let HIST_LOADED = false;
function showTab(name) {
  const clean = name === 'clean';
  document.getElementById('tab_clean').classList.toggle('active', clean);
  document.getElementById('tab_hist').classList.toggle('active', !clean);
  document.getElementById('view_clean').style.display = clean ? '' : 'none';
  document.getElementById('view_hist').style.display = clean ? 'none' : '';
  document.getElementById('bar').style.display = clean ? 'flex' : 'none';
  if (!clean && !HIST_LOADED) { HIST_LOADED = true; loadHistory(); }
}

function fmtDate(iso) {
  if (!iso) return '(date inconnue)';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString('fr-FR', {day:'2-digit', month:'short', year:'numeric',
    hour:'2-digit', minute:'2-digit'});
}

async function loadHistory() {
  const out = document.getElementById('hist_out');
  const d = await fetch('/api/backups').then(r => r.json()).catch(() => ({backups:[]}));
  const list = d.backups || [];
  if (!list.length) {
    out.className = 'msg';
    out.textContent = 'Aucun nettoyage enregistre pour le moment. '
      + 'Les sauvegardes apparaitront ici apres ta premiere suppression.';
    return;
  }
  out.className = 'hist';
  out.innerHTML = list.map(b => `
    <div class="hcard" onclick="openBackup('${esc(b.file)}')">
      <div class="d">${fmtDate(b.deleted_at)}</div>
      <div class="c">${b.count} entree(s) supprimee(s)${b.error ? ' &middot; ' + esc(b.error) : ''}</div>
    </div>`).join('');
}

async function openBackup(file) {
  const d = await fetch('/api/backup?file=' + encodeURIComponent(file))
    .then(r => r.json()).catch(() => ({error: 'lecture impossible'}));
  const root = document.getElementById('modal_root');
  if (d.error) { alert('Erreur : ' + d.error); return; }
  const entries = d.entries || [];
  root.innerHTML = `
    <div class="overlay" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <div class="mh"><h2>Nettoyage du ${fmtDate(d.deleted_at)}</h2>
          <button class="x" onclick="closeModal()">&times;</button></div>
        <div class="msub">${entries.length} entree(s) supprimee(s) de Seerr</div>
        <div class="re-note"><b>Redemander</b> ne restaure pas le fichier : ca recree
          seulement une demande dans Seerr, comme si tu cliquais &laquo; Demander &raquo;.
          Utile uniquement si le media existe encore quelque part et que tu l'as
          efface par erreur.</div>
        <div id="drows"></div>
      </div>
    </div>`;
  const rows = document.getElementById('drows');
  rows.innerHTML = entries.map((e, i) => `
    <div class="drow">
      ${e.poster ? `<img src="${esc(e.poster)}" alt="">`
                 : `<div class="np">${esc(e.title || '?')}</div>`}
      <div class="info">
        <div class="dt">${esc(e.title || '(sans titre)')}</div>
        <div class="dm">${e.type==='tv'?'Serie':'Film'}${e.year?' &middot; '+e.year:''}
          &middot; <span class="badge ${cls(e.status)}">${esc(e.status||'?')}</span></div>
      </div>
      <button class="re" data-i="${i}">Redemander</button>
    </div>`).join('');
  rows.querySelectorAll('.re').forEach(btn => btn.onclick = async () => {
    const e = entries[+btn.dataset.i];
    btn.disabled = true; btn.textContent = '...';
    const r = await fetch('/api/rerequest', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({entry: e})
    }).then(r => r.json()).catch(() => ({ok:false, msg:'erreur reseau'}));
    btn.textContent = r.ok ? 'Redemande OK' : ('Echec : ' + r.msg);
    if (!r.ok) btn.disabled = false;
  });
}
function closeModal() { document.getElementById('modal_root').innerHTML = ''; }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
</script></body></html>"""


# ---------------------------------------------------------------------
#  SERVEUR
# ---------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _origin_ok(self):
        """Anti-CSRF : un site web ouvert dans le navigateur peut envoyer
        des POST 'a l'aveugle' vers 127.0.0.1. Les navigateurs joignent
        toujours l'en-tete Origin aux POST inter-sites : s'il est present
        et ne correspond pas a notre propre adresse, on refuse. Les outils
        hors navigateur (curl, cron) n'envoient pas d'Origin et passent."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return origin in (f"http://{host}", f"https://{host}")

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        if self.path == "/":
            if not config_is_complete():
                self.send_response(302)
                self.send_header("Location", "/config")
                self.end_headers()
                return
            self._send(200, MAIN_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/config":
            page = CONFIG_PAGE.replace("__CONFIG_JSON__", json.dumps(CONFIG or {}))
            self._send(200, page.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/scan":
            print("  Scan en cours...")
            try:
                data = run_scan()
                s = data["stats"]
                print(f"  Radarr {s['radarr']} - Sonarr {s['sonarr']} - "
                      f"Jellyfin {s['jellyfin']} - Seerr {s['seerr']} "
                      f"=> {s['ghosts']} fantomes")
            except ScanError as e:
                print(f"  ERREUR : {e}")
                data = {"error": str(e)}
            self._send(200, json.dumps(data).encode(), "application/json")
        elif self.path == "/api/backups":
            self._send(200, json.dumps({"backups": list_backups()}).encode(),
                       "application/json")
        elif self.path.startswith("/api/backup?"):
            from urllib.parse import parse_qs, urlparse
            name = parse_qs(urlparse(self.path).query).get("file", [""])[0]
            try:
                data = read_backup(name)
            except ScanError as e:
                data = {"error": str(e)}
            self._send(200, json.dumps(data).encode(), "application/json")
        else:
            self._send(404, b"404", "text/plain")

    def do_POST(self):
        if not self._origin_ok():
            self._send(403, b"Origine refusee", "text/plain")
            return
        if self.path == "/api/test":
            b = self._json_body()
            ok, msg = test_endpoint(b.get("kind"), b.get("url", ""), b.get("key", ""))
            self._send(200, json.dumps({"ok": ok, "message": msg}).encode(),
                       "application/json")
        elif self.path == "/api/save":
            save_config(self._json_body())
            print(f"  Config enregistree : {config_path()}")
            self._send(200, json.dumps({"ok": True}).encode(), "application/json")
        elif self.path == "/api/delete":
            entries = self._json_body().get("entries", [])
            print(f"  Suppression de {len(entries)} entree(s)...")
            out = delete_media(entries)
            ok = sum(1 for r in out["results"] if r["ok"])
            print(f"  {ok} OK, {len(out['results']) - ok} echec(s)")
            self._send(200, json.dumps(out).encode(), "application/json")
        elif self.path == "/api/rerequest":
            entry = self._json_body().get("entry", {})
            print(f"  Re-demande : {entry.get('title', '?')}")
            res = rerequest_media(entry)
            self._send(200, json.dumps(res).encode(), "application/json")
        else:
            self._send(404, b"404", "text/plain")


def scan_only(as_json):
    """Mode cron/terminal : scanne et rapporte, ne supprime jamais.
    Codes retour : 0 = aucun fantome, 1 = fantomes trouves, 2 = erreur."""
    load_config()
    if not config_is_complete():
        print("Configuration incomplete : lance d'abord le script sans "
              "argument pour la faire dans le navigateur.")
        sys.exit(2)
    try:
        res = run_scan()
    except ScanError as e:
        print(f"Erreur de scan : {e}")
        sys.exit(2)
    if as_json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        s = res["stats"]
        print(f"Radarr : {s['radarr']} | Sonarr : {s['sonarr']} | "
              f"Jellyfin : {s['jellyfin']} | Seerr : {s['seerr']}")
        print(f"Fantomes : {s['ghosts']}")
        for g in res["ghosts"]:
            y = f" ({g['year']})" if g.get("year") else ""
            print(f"  - [{g['type']}] {g.get('title', '?')}{y}  "
                  f"seerr_id={g['id']}")
    sys.exit(1 if res["ghosts"] else 0)


if __name__ == "__main__":
    if "--scan-only" in sys.argv:
        scan_only("--json" in sys.argv)

    load_config()
    server = None
    for p in range(PORT, PORT + 10):
        try:
            server = HTTPServer((BIND_HOST, p), Handler)
            PORT = p
            break
        except OSError:
            continue
    if server is None:
        print(f"Impossible d'ouvrir un port entre {PORT} et {PORT + 9} "
              f"(deja utilises ?). Ferme l'instance existante et relance.")
        sys.exit(2)
    url = f"http://127.0.0.1:{PORT}"
    configured = config_is_complete()
    print(f"\n  Seerr Cleaner — interface sur {url}")
    if not configured:
        print("  Premiere utilisation : configure tes URL et cles dans le navigateur.")
    print("  (Ctrl+C pour quitter)\n")
    threading.Timer(1.0, lambda: webbrowser.open(
        url if configured else url + "/config")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Arret.\n")