# Capago Alger — moniteur de rendez-vous visa

Surveille en continu le site **https://appointment-alg.capago.eu/** et **t'alerte
dès que « Long séjour (> 90 jours) → Études » apparaît** dans *Votre projet*.

- ✅ Lecture seule d'un endpoint public — ne réserve rien, ne contourne aucun captcha.
- ✅ Python pur (aucun `pip install`).
- ✅ Alertes : notification bureau + son + Telegram (téléphone) + email + ouverture auto du site.
- ✅ Tourne 24/7 via `systemd --user`, redémarre tout seul, démarre au boot.

## Statut actuel (vérifié le 17/06/2026)

`Long séjour → Études` est **FERMÉ**. « Études » n'existe aujourd'hui que sous
*Court séjour*. C'est exactement ce que ce moniteur attend de voir changer.

---

## 1. Configuration

Édite `config.env`. Le minimum marche déjà (notif bureau + son + ouverture navigateur).
Pour être prévenu **sur ton téléphone même absent du PC**, configure Telegram :

### Telegram (recommandé, ~2 min)
1. Sur Telegram, écris à **@BotFather** → `/newbot` → choisis un nom. Il te donne un
   **token** (genre `1234567890:AAH...`). Mets-le dans `TELEGRAM_TOKEN`.
2. Écris un message à ton nouveau bot (n'importe quoi), puis ouvre dans un navigateur :
   `https://api.telegram.org/bot<TON_TOKEN>/getUpdates`
   Cherche `"chat":{"id":123456789` → c'est ton `TELEGRAM_CHAT_ID`.
3. Colle les deux valeurs dans `config.env`.

### Email (optionnel, secours)
Remplis `SMTP_HOST/PORT/USER/PASS`. Gmail : `smtp.gmail.com` port `587` + un
**mot de passe d'application** (pas ton mot de passe normal).

---

## 2. Test rapide (avant de lancer en service)

```bash
cd ~/projects/capago-monitor
# Force une "fausse ouverture" en surveillant Études court-séjour (ouvert aujourd'hui)
WATCH_TARGETS=short_stay_visa:study python3 monitor.py
```
Tu dois voir une notification + entendre le son + (si configuré) recevoir un Telegram.
`Ctrl+C` pour arrêter, puis lance pour de vrai (sans le `WATCH_TARGETS=...`).

---

## 3. Lancer 24/7 avec systemd

```bash
mkdir -p ~/.config/systemd/user
cp ~/projects/capago-monitor/capago-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now capago-monitor.service

# pour que ça tourne même après déconnexion / au boot :
sudo loginctl enable-linger $USER
```

### Surveiller / gérer
```bash
systemctl --user status capago-monitor.service     # état
journalctl --user -u capago-monitor.service -f     # logs en direct
systemctl --user restart capago-monitor.service    # après modif de config.env
systemctl --user stop capago-monitor.service       # arrêter
```

> Notifications/son sous **Wayland** : si rien n'apparaît, ajoute dans le `.service`
> `Environment=WAYLAND_DISPLAY=wayland-0` puis `daemon-reload` + `restart`.

---

## Options utiles (`config.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `WATCH_TARGETS` | `long_stay_visa:study` | quoi surveiller (`stay:reason`, plusieurs séparés par `,`) |
| `POLL_SECONDS` | `90` | fréquence de vérification |
| `REALERT_SECONDS` | `1800` | re-alerter toutes les 30 min tant que c'est ouvert (0 = une seule fois) |
| `OPEN_BROWSER` | `1` | ouvre le site automatiquement à la détection |

IDs valides — *stay* : `transit_visa`, `short_stay_visa`, `long_stay_visa` ·
*reason* : `study`, `work`, `family`, `family_minor`, `placement`, `return`, `visitor`, `tourism`, `medical`, `business`, `establishment`.

## Comment ça marche (technique)
Toutes les ~90 s, GET sur
`https://visa-fr-dz.capago.eu/rendezvous_alger/WebSite_getApplicableVisaTypeList?capago_center_id=capago_ALG`,
puis on regarde si un sous-élément `study` est apparu sous `long_stay_visa`. L'alerte
ne se déclenche qu'à la **transition fermé → ouvert** (pas de spam). L'état est gardé
dans `state.json`.

---

# Bonus : moniteur Crous (trouverunlogement.lescrous.fr)

`crous-monitor.py` surveille des régions sur le site Crous et **t'alerte quand une
nouvelle annonce apparaît** (id jamais vu auparavant). Configuration multi-régions.

⚠️ Le site Crous sert souvent une page "Vous êtes trop nombreux !" en cas de forte
affluence — le moniteur détecte ça et recule sans broncher, sans envoyer de fausses
alertes.

### Régions surveillées
Édite `crous-regions.txt` (une région par ligne, format `id|nom|api_url|viewer_url`).
Les deux lignes par défaut couvrent Aubière et Clermont-Ferrand sur l'outil `/tools/47`.

### IMPORTANT — trouver la vraie URL JSON de recherche
Le chemin `/api/tools/47/search?bounds=...` est le *best guess* par défaut. Pour
remplacer par la vraie URL :
1. Ouvre `https://trouverunlogement.lescrous.fr/tools/47/search?bounds=...&locationName=Aubière`
   dans ton navigateur.
2. DevTools → **Network** → filtre **Fetch/XHR** → recharge la page.
3. Trouve la requête qui renvoie du JSON contenant une **liste de résidences** (pas
   le `/photon/api?q=...` qui est juste l'autocomplétion de lieu).
4. Copie son URL complète dans la 3ᵉ colonne de `crous-regions.txt`.

### Lancer 24/7 avec systemd
```bash
cp ~/projects/capago-monitor/crous-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now crous-monitor.service
```
Réutilise les mêmes canaux (Telegram / SMTP) que `config.env` déjà configuré pour le
monitor visa — le script Crous les lit directement.

### Test rapide
```bash
CROUS_RUN_ONCE=1 python3 crous-monitor.py
```
