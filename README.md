# Walsroder Zeitung E-Paper → Nextcloud

Lädt die tägliche PDF-Ausgabe der Walsroder Zeitung hinter der plenigo-Paywall
(mit deinem legitimen Abo-Zugang) und legt sie per WebDAV in deiner Nextcloud ab.

## Wie der Login funktioniert

`mein.wz-net.de/login` bettet ein Login-Formular von **plenigo**
(`checkout.plenigo.com`) in einem iframe ein. Nach erfolgreicher Anmeldung
setzt `mein.wz-net.de` Session-Cookies für die Domain `wz-net.de`, mit denen
dann `www.wz-net.de/.../epaper/...pdf` abrufbar ist.

Weil das Formular pro Aufruf frische CSRF-/Flow-Tokens hat und der Erfolg über
ein JavaScript-Event läuft, nutzt das Script einen **headless Chromium
(Playwright)**. Die Session wird in `state.json` gecacht – danach wird nur noch
bei abgelaufener Session neu eingeloggt.

## E-Paper-URL

Die "Gesamtausgabe" liegt unter:

```
https://www.wz-net.de/sites/default/files/content/epaper/<JJJJ>/<JJJJMMTT>_wz.pdf
```

Die Datei wird unter ihrem Originalnamen `<JJJJMMTT>_wz.pdf` abgelegt (lokal wie
in der Nextcloud; anpassbar über `SAVE_NAME_TMPL` in `download_wz.py`).
Sonntags gibt es keine Ausgabe (HTTP 404) – das Script überspringt den Tag.

## Installation (Ubuntu-Server)

Geprüft für Ubuntu Server 22.04 / 24.04.

```bash
# 1. Systempakete (auf Ubuntu Server nicht vorinstalliert)
sudo apt update
sudo apt install -y git python3-venv python3-pip

# 2. Code holen. Das Repo ist privat -> Git fragt nach Benutzername + Personal
#    Access Token. Alternativ das Verzeichnis per scp/rsync hochladen.
sudo git clone https://github.com/sascha224/wz-epaper.git /opt/wz-epaper

# 3. Dienst-Benutzer anlegen und Verzeichnis uebergeben
sudo useradd --system --home-dir /opt/wz-epaper --shell /usr/sbin/nologin wz
sudo chown -R wz:wz /opt/wz-epaper
cd /opt/wz-epaper

# 4. venv + Python-Abhaengigkeiten (als wz)
sudo -u wz python3 -m venv .venv
sudo -u wz .venv/bin/pip install --upgrade pip
sudo -u wz .venv/bin/pip install -r requirements.txt

# 5. Chromium fuer Playwright: System-Libs als root, Browser als wz
sudo .venv/bin/playwright install-deps chromium
sudo -u wz .venv/bin/playwright install chromium
```

> Läuft ohnehin alles als root, gehen Schritt 4/5 kürzer mit
> `.venv/bin/playwright install --with-deps chromium` (zieht die System-Libs
> per apt gleich mit). `--with-deps` braucht zwingend root.

## Konfiguration

```bash
sudo -u wz cp .env.example .env
sudo -u wz chmod 600 .env
sudo -u wz nano .env
```

Pflichtfelder: `WZ_USERNAME`, `WZ_PASSWORD`, `NC_WEBDAV_URL`, `NC_USERNAME`, `NC_PASSWORD`.

- **`NC_WEBDAV_URL`**: `https://<deine-cloud>/remote.php/dav/files/<NC_USERNAME>`
- **`NC_PASSWORD`**: in Nextcloud unter *Einstellungen → Sicherheit →
  „App-Passwort erstellen"* erzeugen (nicht dein Hauptpasswort).
- **`NC_TARGET_DIR`**: z. B. `Walsroder Zeitung/{year}` – Ordner werden
  automatisch angelegt.

## Test

```bash
# Headless-Testlauf für heute, ohne Upload:
sudo -u wz .venv/bin/python download_wz.py --no-upload -v

# Bestimmtes Datum:
sudo -u wz .venv/bin/python download_wz.py --date 2026-09-08 --no-upload -v

# Voller Lauf inkl. Nextcloud:
sudo -u wz .venv/bin/python download_wz.py -v
```

Beim ersten Lauf wird `state.json` angelegt. Danach sollten Folgeläufe ohne
erneuten Login durchlaufen. (`--headful` nur mit grafischer Umgebung, siehe
unten.)

## Automatisieren (cron)

```bash
sudo crontab -u wz -e
# Inhalt aus crontab.example übernehmen
```

`crontab.example` startet Mo–Sa um 06:00, hängt die Ausgabe an
`/opt/wz-epaper/wz-epaper.log` an (Pfad anpassbar) und schickt bei Fehler-Exit
eine Mail (`MAILTO` setzen; braucht einen MTA wie `postfix` oder `msmtp`). Das
Script wiederholt selbst (`WZ_RETRIES`), falls die Ausgabe morgens noch nicht
online ist. Der cron-Dienst läuft auf Ubuntu per Default (`systemctl status cron`).

### Zeitzone

cron nutzt die **Systemzeitzone**. Viele Server (Cloud/VPS) stehen auf **UTC** –
dann feuert `0 6` um 06:00 UTC, nicht lokal. Prüfen und umstellen:

```bash
timedatectl                                  # aktuelle Zone
sudo timedatectl set-timezone Europe/Berlin
sudo systemctl restart cron                  # cron liest die Zone beim Start neu
```

Alternativ nur für diesen Job, System bleibt auf UTC – in die crontab **über**
die Job-Zeile:

```
CRON_TZ=Europe/Berlin
```

## Verhalten / Details

- **Sonntag** wird übersprungen (keine Ausgabe). Mit `--sunday` erzwingbar.
- Ist die PDF morgens noch nicht online (HTTP 404), wartet das Script
  `WZ_RETRY_WAIT` Sekunden und versucht es bis zu `WZ_RETRIES`-mal erneut.
- Heruntergeladene Bytes werden auf die PDF-Signatur `%PDF-` geprüft; kommt
  stattdessen HTML zurück (Paywall/Session abgelaufen), wird einmal neu
  eingeloggt.
- Existiert die lokale Datei schon (`WZ_LOCAL_DIR` gesetzt), wird der Download
  übersprungen – außer mit `--force`.
- Exit-Codes siehe Kopf von `download_wz.py` (cron meldet Fehler-Exit per Mail).

## Wenn der Login bricht

Ändert plenigo das Formular, schlägt der Login mit Exit 3 fehl. Dann:

```bash
.venv/bin/python download_wz.py --headful -v
```

und im sichtbaren Browser prüfen, welche Feld-IDs/Buttons sich geändert haben
(`_do_login()` in `download_wz.py` anpassen: aktuell `#login_form__username`,
`#login_form__password`, Button „Anmelden").

`--headful` braucht eine grafische Umgebung. Auf einem Server ohne Desktop
entweder lokal auf dem Arbeitsrechner testen oder mit `xvfb` starten:

```bash
sudo apt install -y xvfb
sudo -u wz xvfb-run .venv/bin/python download_wz.py --headful -v
```

## Dateien

| Datei | Zweck |
|---|---|
| `download_wz.py` | Hauptscript |
| `.env` / `.env.example` | Zugangsdaten & Optionen |
| `requirements.txt` | Python-Abhängigkeiten |
| `state.json` | gecachte Browser-Session (wird angelegt) |
| `crontab.example` | cron-Eintrag zum Übernehmen |
