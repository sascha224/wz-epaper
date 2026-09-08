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

## Installation (Linux-Server)

```bash
sudo useradd -r -m -d /opt/wz-epaper wz
sudo -u wz -H bash
cd /opt/wz-epaper

git clone <dieses-verzeichnis> .    # oder Dateien herkopieren
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium   # lädt Browser + Systemlibs
```

> `--with-deps` braucht einmalig root für die Systempakete. Ohne root:
> `.venv/bin/playwright install chromium` und die
> [benötigten Libs](https://playwright.dev/python/docs/browsers#install-system-dependencies)
> selbst nachinstallieren.

## Konfiguration

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Pflichtfelder: `WZ_USERNAME`, `WZ_PASSWORD`, `NC_WEBDAV_URL`, `NC_USERNAME`, `NC_PASSWORD`.

- **`NC_WEBDAV_URL`**: `https://<deine-cloud>/remote.php/dav/files/<NC_USERNAME>`
- **`NC_PASSWORD`**: in Nextcloud unter *Einstellungen → Sicherheit →
  „App-Passwort erstellen"* erzeugen (nicht dein Hauptpasswort).
- **`NC_TARGET_DIR`**: z. B. `Walsroder Zeitung/{year}` – Ordner werden
  automatisch angelegt.

## Test

```bash
# Sichtbarer Browser, einmaliger Lauf für heute, ohne Upload:
.venv/bin/python download_wz.py --headful --no-upload -v

# Bestimmtes Datum:
.venv/bin/python download_wz.py --date 2026-09-08 -v

# Voller Lauf inkl. Nextcloud:
.venv/bin/python download_wz.py -v
```

Beim ersten Lauf wird `state.json` angelegt. Danach sollten Folgeläufe ohne
erneuten Login durchlaufen.

## Automatisieren

### Variante A – systemd-Timer (empfohlen)

```bash
sudo cp systemd/wz-epaper.service systemd/wz-epaper.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wz-epaper.timer

systemctl list-timers wz-epaper.timer      # nächste Ausführung prüfen
journalctl -u wz-epaper.service -f          # Log ansehen
sudo systemctl start wz-epaper.service      # manuell auslösen
```

Der Service erwartet die venv unter `/opt/wz-epaper/.venv` und die `.env` im
`WorkingDirectory`. `User=` ggf. anpassen.

### Variante B – cron

```bash
crontab -e
# Inhalt aus crontab.example übernehmen
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
- Exit-Codes siehe Kopf von `download_wz.py` (cron/systemd melden Fehler).

## Wenn der Login bricht

Ändert plenigo das Formular, schlägt der Login mit Exit 3 fehl. Dann:

```bash
.venv/bin/python download_wz.py --headful -v
```

und im sichtbaren Browser prüfen, welche Feld-IDs/Buttons sich geändert haben
(`_do_login()` in `download_wz.py` anpassen: aktuell `#login_form__username`,
`#login_form__password`, Button „Anmelden").

## Dateien

| Datei | Zweck |
|---|---|
| `download_wz.py` | Hauptscript |
| `.env` / `.env.example` | Zugangsdaten & Optionen |
| `requirements.txt` | Python-Abhängigkeiten |
| `state.json` | gecachte Browser-Session (wird angelegt) |
| `systemd/` | service + timer |
| `crontab.example` | cron-Alternative |
