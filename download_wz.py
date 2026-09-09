#!/usr/bin/env python3
"""
download_wz.py — Walsroder Zeitung E-Paper (PDF) automatisch laden und nach Nextcloud schieben.

Ablauf:
  1. Zieldatum bestimmen (Standard: heute; Sonntag wird uebersprungen, keine Ausgabe).
  2. PDF-URL bauen:  https://www.wz-net.de/sites/default/files/content/epaper/<JJJJ>/<JJJJMMTT>_wz.pdf
  3. Mit gecachter Browser-Session versuchen, die PDF zu laden.
     Wenn nicht eingeloggt / Paywall greift -> ueber plenigo (mein.wz-net.de/login) anmelden,
     Session in state.json cachen, erneut laden.
  4. PDF lokal speichern (optional) und per WebDAV in die Nextcloud hochladen.

Konfiguration ueber Umgebungsvariablen oder eine .env-Datei neben diesem Script
(siehe .env.example). CLI-Flags ueberschreiben die .env-Werte.

Exit-Codes:
  0  Erfolg  (oder: keine Ausgabe an diesem Tag)
  2  Konfigurationsfehler
  3  Login fehlgeschlagen (falsche Zugangsdaten / plenigo-Flow geaendert)
  4  PDF noch nicht veroeffentlicht nach allen Retries
  5  Download-/Validierungsfehler (kein PDF zurueckbekommen)
  6  Nextcloud-Upload fehlgeschlagen
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright fehlt.  ->  pip install -r requirements.txt  &&  playwright install --with-deps chromium")

LOGIN_URL = "https://mein.wz-net.de/login"
WARMUP_URL = "https://www.wz-net.de/"
# Reale "Gesamtausgabe" (ermittelt aus der E-Paper-Seite): OHNE "Walsroderzeitung_"-Praefix.
# Beispiel: .../content/epaper/2026/20260908_wz.pdf
PDF_URL_TMPL = (
    "https://www.wz-net.de/sites/default/files/content/epaper/"
    "{year}/{ymd}_wz.pdf"
)
# Datei wird unter ihrem Originalnamen abgelegt (lokal wie in der Nextcloud).
SAVE_NAME_TMPL = "{ymd}_wz.pdf"
PDF_MAGIC = b"%PDF-"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

log = logging.getLogger("wz")


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    """Minimaler .env-Parser (KEY=VALUE, # Kommentare). Setzt nur, was noch nicht gesetzt ist."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


def need(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log.error("Pflicht-Konfiguration fehlt: %s", name)
        sys.exit(2)
    return val


# --------------------------------------------------------------------------- #
# Fehlerklassen
# --------------------------------------------------------------------------- #
class NotPublishedYet(Exception):
    """PDF liefert 404 – Ausgabe ist noch nicht online."""


class LoginFailed(Exception):
    pass


# --------------------------------------------------------------------------- #
# Browser / plenigo-Login
# --------------------------------------------------------------------------- #
def _new_context(browser, state_file: Path):
    kwargs = dict(user_agent=UA, accept_downloads=True, locale="de-DE")
    if state_file.is_file():
        kwargs["storage_state"] = str(state_file)
        log.debug("Nutze gecachte Session aus %s", state_file)
    return browser.new_context(**kwargs)


def _try_download(context, pdf_url: str):
    """Versucht die PDF mit den aktuellen Cookies zu laden.
    -> bytes bei Erfolg, None wenn (vermutlich) Paywall/nicht eingeloggt.
    -> wirft NotPublishedYet bei 404."""
    resp = context.request.get(pdf_url, headers={"Accept": "application/pdf,*/*"}, max_redirects=10)
    body = resp.body()
    ctype = resp.headers.get("content-type", "")
    log.info("GET %s -> HTTP %s (%s, %d Bytes)", pdf_url, resp.status, ctype or "?", len(body))

    if resp.status == 404:
        raise NotPublishedYet(pdf_url)
    if resp.status == 200 and body[:5] == PDF_MAGIC:
        return body
    # 200 mit HTML (Login-Seite) oder 401/403 -> nicht autorisiert
    return None


def _do_login(page, username: str, password: str) -> None:
    log.info("Melde bei plenigo an (%s) …", LOGIN_URL)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

    frame = page.frame_locator("iframe[src*='plenigo.com']")
    user_field = frame.locator("#login_form__username")
    try:
        user_field.wait_for(state="visible", timeout=30000)
    except PWTimeout:
        raise LoginFailed("plenigo-Login-iframe / Formularfeld nicht gefunden – Flow evtl. geaendert")

    user_field.fill(username)
    frame.locator("#login_form__password").fill(password)
    try:
        frame.locator("#login_form_rememberMe").check(timeout=3000)
    except PWTimeout:
        pass  # Checkbox optional

    frame.get_by_role("button", name="Anmelden").click()

    # Erfolg: Parent-Seite leitet auf /login?...token=... um und danach weiter.
    try:
        page.wait_for_url("**token=**", timeout=30000)
    except PWTimeout:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass

    # Noch immer auf der Login-Seite ohne Token -> Zugangsdaten falsch o. Flow kaputt
    if "/login" in page.url and "token=" not in page.url:
        body_txt = ""
        try:
            body_txt = frame.locator("body").inner_text(timeout=3000)[:300]
        except Exception:
            pass
        raise LoginFailed(f"Login nicht bestaetigt. Seite: {page.url}  Hinweis: {body_txt!r}")

    log.info("Login ok (URL jetzt: %s)", page.url)


def fetch_pdf(state_file: Path, username: str, password: str, pdf_url: str, headful: bool) -> bytes:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = _new_context(browser, state_file)
        page = context.new_page()
        try:
            pdf = _try_download(context, pdf_url)
            if pdf is None:
                log.info("Nicht autorisiert / Paywall – starte Anmeldung.")
                _do_login(page, username, password)
                context.storage_state(path=str(state_file))
                # www-Domain einmal besuchen, damit evtl. SSO-Cookies synchronisiert werden
                try:
                    page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=30000)
                except PWTimeout:
                    pass
                pdf = _try_download(context, pdf_url)
            # Session (ggf. aufgefrischt) sichern
            context.storage_state(path=str(state_file))
            return pdf
        finally:
            browser.close()


# --------------------------------------------------------------------------- #
# Nextcloud (WebDAV)
# --------------------------------------------------------------------------- #
def upload_nextcloud(webdav_base: str, user: str, pw: str, target_dir: str,
                     filename: str, data: bytes) -> None:
    base = webdav_base.rstrip("/")
    auth = (user, pw)
    sess = requests.Session()
    sess.auth = auth

    # Zielverzeichnisse rekursiv anlegen (MKCOL ist nicht rekursiv)
    parts = [seg for seg in target_dir.strip("/").split("/") if seg]
    cur = base
    for seg in parts:
        cur = f"{cur}/{quote(seg)}"
        r = sess.request("MKCOL", cur, timeout=30)
        if r.status_code in (201,):
            log.debug("angelegt: %s", cur)
        elif r.status_code in (405, 301, 409):
            log.debug("existiert bereits: %s (%s)", cur, r.status_code)
        else:
            log.warning("MKCOL %s -> HTTP %s", cur, r.status_code)

    url = f"{base}/" + "/".join(quote(s) for s in parts + [filename])
    r = sess.put(url, data=data, headers={"Content-Type": "application/pdf"}, timeout=120)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"PUT {url} -> HTTP {r.status_code}: {r.text[:300]}")
    log.info("Nextcloud-Upload ok: %s (HTTP %s)", url, r.status_code)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv):
    ap = argparse.ArgumentParser(description="Walsroder Zeitung E-Paper laden + nach Nextcloud")
    ap.add_argument("--date", help="Zieldatum JJJJ-MM-TT (Standard: heute)")
    ap.add_argument("--retries", type=int, default=int(os.environ.get("WZ_RETRIES", "6")),
                    help="Wiederholungen, falls PDF noch nicht online (404)")
    ap.add_argument("--retry-wait", type=int, default=int(os.environ.get("WZ_RETRY_WAIT", "600")),
                    help="Wartezeit zwischen den Retries in Sekunden")
    ap.add_argument("--no-upload", action="store_true", help="nur herunterladen, kein Nextcloud")
    ap.add_argument("--force", action="store_true", help="auch laden, wenn lokal schon vorhanden")
    ap.add_argument("--sunday", action="store_true", help="auch sonntags versuchen")
    ap.add_argument("--headful", action="store_true", help="Browser sichtbar (Debug)")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    # .env zuerst laden, damit die argparse-Defaults (WZ_RETRIES/WZ_RETRY_WAIT)
    # die .env-Werte sehen. Echte Umgebungsvariablen haben Vorrang (setdefault),
    # explizite CLI-Flags gewinnen ueber beides.
    here = Path(__file__).resolve().parent
    load_env_file(here / ".env")

    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    target = (
        dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    )
    if target.weekday() == 6 and not args.sunday:  # 6 = Sonntag
        log.info("%s ist ein Sonntag – keine Ausgabe. Nichts zu tun.", target)
        return 0

    ymd = target.strftime("%Y%m%d")
    pdf_url = PDF_URL_TMPL.format(year=target.strftime("%Y"), ymd=ymd)
    filename = SAVE_NAME_TMPL.format(ymd=ymd)

    username = need("WZ_USERNAME")
    password = need("WZ_PASSWORD")
    state_file = Path(os.environ.get("WZ_STATE_FILE", here / "state.json")).expanduser()

    local_dir_cfg = os.environ.get("WZ_LOCAL_DIR", "").strip()
    local_dir = Path(local_dir_cfg).expanduser() if local_dir_cfg else None
    local_path = local_dir / filename if local_dir else None

    if local_path and local_path.is_file() and local_path.stat().st_size > 1000 and not args.force:
        log.info("Lokal bereits vorhanden: %s  (--force zum Erzwingen)", local_path)
        pdf_bytes = local_path.read_bytes()
    else:
        # ---- Download mit Retry-Schleife fuer "noch nicht veroeffentlicht" ----
        pdf_bytes = None
        attempts = args.retries + 1
        for i in range(1, attempts + 1):
            try:
                pdf_bytes = fetch_pdf(state_file, username, password, pdf_url, args.headful)
                break
            except NotPublishedYet:
                if i == attempts:
                    log.error("PDF nach %d Versuchen noch nicht online: %s", attempts, pdf_url)
                    return 4
                log.info("Ausgabe noch nicht online (Versuch %d/%d) – warte %ds.",
                         i, attempts, args.retry_wait)
                time.sleep(args.retry_wait)
            except LoginFailed as e:
                log.error("Login fehlgeschlagen: %s", e)
                return 3

        if not pdf_bytes or pdf_bytes[:5] != PDF_MAGIC:
            log.error("Kein gueltiges PDF erhalten (Paywall? Zugang abgelaufen? Flow geaendert?).")
            return 5

        log.info("PDF geladen: %d Bytes", len(pdf_bytes))
        if local_path:
            local_dir.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(pdf_bytes)
            log.info("Lokal gespeichert: %s", local_path)

    # ---- Upload ----
    if args.no_upload:
        log.info("--no-upload gesetzt, fertig.")
        return 0

    webdav_base = need("NC_WEBDAV_URL")      # z.B. https://cloud.example.de/remote.php/dav/files/sascha
    nc_user = need("NC_USERNAME")
    nc_pass = need("NC_PASSWORD")            # App-Passwort empfohlen
    nc_dir_tmpl = os.environ.get("NC_TARGET_DIR", "Walsroder Zeitung/{year}")
    nc_dir = nc_dir_tmpl.format(year=target.strftime("%Y"), month=target.strftime("%m"))

    try:
        upload_nextcloud(webdav_base, nc_user, nc_pass, nc_dir, filename, pdf_bytes)
    except Exception as e:
        log.error("Nextcloud-Upload fehlgeschlagen: %s", e)
        return 6

    log.info("Fertig: %s", filename)
    return 0


if __name__ == "__main__":
    sys.exit(main())
