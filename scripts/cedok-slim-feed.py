#!/usr/bin/env python3
"""Čedok slim feed pro kakapo.cz — most SFTP → redukce → FTP.

Plný SDovolena feed Čedoku má 3,8 GB (každý hotel stovky <term>) — WEDOS
nezvládne stáhnout ani sparsovat. Tenhle skript na Malině:

 1. streamuje sDovolena_cedok_cs.xml ze SFTP (curl | iterparse, nic se
    neukládá celé),
 2. z každého <tour> nechá hotel + max MAX_OFFERS nejlevnějších BUDOUCÍCH
    termínů (jeden na kombinaci strava×doprava×odjezd, s diverzitou dopravy),
 3. do <term> doplní <meal>/<transport>/<airport> (strava z meal= parametru
    purchase_url, doprava z transport id) a přepíše <id> na
    "{tour_id}-{term_id}" (unikátní klíč offers je (feed_id, external_id) —
    holé term id se opakuje napříč hotely!),
 4. výstup ve STEJNÉ <tours><tour> struktuře (preset sdovolena_hotels
    + pár polí navíc) nahraje FTP na kakapo.cz /feeds/ + CF purge.

Použití:
  python3 cedok-slim-feed.py                 # plný běh + upload
  python3 cedok-slim-feed.py --limit-mb 50   # test na prvních 50 MB
  python3 cedok-slim-feed.py --no-upload     # jen vygenerovat lokálně
Creds: ~/ai/credentials/.kakapo-credentials.env (CEDOK_SFTP_*, FTP_*, CF_*).
"""
import argparse
import base64
import datetime
import io
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

ENV_FILE = os.environ.get("KAKAPO_ENV_FILE", "/home/kenny/ai/credentials/.kakapo-credentials.env")
OUT_DEFAULT = "/tmp/cedok-slim.xml"  # tmpfs na Malině (~100 MB)
LM_FILE = "sDovolena_last_minute_cedok_cs.xml"  # SFTP, obnova několikrát denně
MAIN_FILE_ENV = "CEDOK_SFTP_FILE"
FAR_CACHE = os.path.expanduser("~/.cache/kakapo/cedok-far.json")  # vzdálené termíny pro rychlé běhy
REMOTE_PATH = "/feeds/cedok-slim.xml"  # relativně k WP rootu (FTP root)
FEED_URL = "https://kakapo.cz/feeds/cedok-slim.xml"

MIN_HOTELS = 2500       # pojistka: míň hotelů = chyba zdroje, feed nenahrávat
MAX_OFFERS = 12         # nabídek na hotel
MAX_PER_TRANSPORT = 6   # diverzita: max N nabídek stejné dopravy
MAX_PHOTOS = 8
TRANSPORT_LABEL = {"1": "vlastní", "2": "letecky", "4": "autobusem"}

# Popis = spojené sekce accommodation_desc v tomto pořadí. Sekce vázané na
# konkrétní cenu/nabídku (Cena zahrnuje, Fakultativní služby, Program zájezdu,
# Informace) záměrně vynechány — rychle zastarávají.
DESC_SECTIONS = ("Výhody nabídky", "Poloha", "Pláž", "Ubytování",
                 "Stravování", "Sport a zábava", "Vybavení")
DESC_MAX_CHARS = 4000
DESC_MIN_SECTION = 60   # kratší sekce = holá adresa/fragment → šum, vynechat


ENV_KEYS = ("CEDOK_SFTP_HOST", "CEDOK_SFTP_USER", "CEDOK_SFTP_PASS", "CEDOK_SFTP_FILE",
            "ATIS_FEED_URL", "KAKAPO_IMPORT_URL", "KAKAPO_IMPORT_SECRET", "CF_ZONE_ID")


def load_env(path=ENV_FILE):
    """Keys from the credentials file (local runs on the Pi), overridden by environment
    variables (GitHub Actions secrets). A missing file is fine in CI."""
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    for k in ENV_KEYS:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def meal_from_purchase_url(url):
    """meal= parametr v purchase_url (např. Sn%C3%ADdan%C4%9B → Snídaně)."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        return (q.get("meal") or [""])[0].strip()
    except ValueError:
        return ""


def offer_link(purchase_url):
    """Odkaz nabídky: purchase_url je instant-booking s cenou zamčenou
    v q= payloadu — jakmile Čedok přecení, booking spadne na „Něco se
    pokazilo". Payload ale nese productUrl (stránka nabídky s ČERSTVOU
    dostupností) → používáme ten; purchase_url jen jako fallback."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(purchase_url).query)["q"][0]
        raw = base64.b64decode(q + "=" * (-len(q) % 4)).decode()
        purl = json.loads(urllib.parse.unquote(raw)).get("productUrl", "")
        if purl.startswith("http"):
            return purl
    except (KeyError, ValueError, json.JSONDecodeError):
        pass
    return purchase_url


def first_text(el, path):
    node = el.find(path)
    return (node.text or "").strip() if node is not None and node.text else ""


def collect_candidates(tour, today, until=None, after=None):
    """Z term_groups vybere cheapest budoucí termín na skupinu
    (board×transport×odjezd). `until`/`after` omezí odjezdy (včetně `until`,
    vyjma `after`) — slouží ke slévání dvou souborů s různou čerstvostí."""
    candidates = {}  # (board, transport, dept) -> nejlevnější term; stejný
    # klíč se ve feedu opakuje ve víc <term_group> (různé pokoje) → dedup
    for group in tour.iter("term_group"):
        board = first_text(group, "board/id")
        transport = first_text(group, "transport/id")
        dept = first_text(group, "dept_places/place")
        best = None
        for term in group.iter("term"):
            start = first_text(term, "start")
            if not start or start < today:
                continue
            if until and start > until:
                continue
            if after and start <= after:
                continue
            try:
                price = int(float(first_text(term, "prices/price/final_price") or 0))
            except ValueError:
                continue
            if price <= 0:
                continue
            purchase = first_text(term, "purchase_url")
            if not purchase.startswith("http"):
                continue
            if best is None or price < best["price"]:
                try:
                    orig = int(float(first_text(term, "prices/price_orig") or 0))
                except ValueError:
                    orig = 0
                best = {
                    "term_id": first_text(term, "id"),
                    "start": start,
                    "end": first_text(term, "end"),
                    "nights": first_text(term, "night_count") or "1",
                    "price": price,
                    "price_orig": orig if orig > price else 0,
                    "purchase_url": purchase,
                    "board": board,
                    "transport": transport,
                    "dept": dept,
                }
        if best:
            key = (board, transport, dept)
            if key not in candidates or best["price"] < candidates[key]["price"]:
                candidates[key] = best
    return candidates


def merge_candidates(*dicts):
    """Slije kandidáty více zdrojů; při shodě klíče vyhraje levnější nabídka."""
    out = {}
    for d in dicts:
        for key, o in d.items():
            if key not in out or o["price"] < out[key]["price"]:
                out[key] = o
    return out


def choose_offers(candidates):
    """Greedy výběr podle ceny s limitem MAX_OFFERS a MAX_PER_TRANSPORT."""
    ordered = sorted(candidates.values(), key=lambda o: o["price"])
    picked, per_transport = [], {}
    for offer in ordered:
        if len(picked) >= MAX_OFFERS:
            break
        t = offer["transport"]
        if per_transport.get(t, 0) >= MAX_PER_TRANSPORT:
            continue
        per_transport[t] = per_transport.get(t, 0) + 1
        picked.append(offer)
    return picked


def terms_xml(tour_id, offers):
    parts = ["<terms>"]
    w = parts.append
    for o in offers:
        meal = meal_from_purchase_url(o["purchase_url"])
        transport = TRANSPORT_LABEL.get(o["transport"], "")
        airport = o["dept"] if o["transport"] == "2" else ""
        # term id je jen z dat → stejný termín v jiné skupině (strava/doprava/
        # odjezd) by kolidoval; přidat sufix skupiny (upsert klíč = external_id)
        grp = f"b{o['board'] or '0'}t{o['transport'] or '0'}"
        if o["dept"]:
            grp += "-" + "".join(c for c in o["dept"].lower() if c.isalnum())[:6]
        w("<term>")
        w(f"<id>{escape(tour_id)}-{escape(o['term_id'])}-{grp}</id>")
        w(f"<start>{escape(o['start'])}</start>")
        w(f"<end>{escape(o['end'])}</end>")
        w(f"<night_count>{escape(o['nights'])}</night_count>")
        w(f"<final_price>{o['price']}</final_price>")
        if o["price_orig"]:
            w(f"<price_orig>{o['price_orig']}</price_orig>")
        w(f"<meal>{escape(meal)}</meal>")
        w(f"<transport>{escape(transport)}</transport>")
        w(f"<airport>{escape(airport)}</airport>")
        w(f"<purchase_url>{escape(offer_link(o['purchase_url']))}</purchase_url>")
        w("</term>")
    w("</terms>")
    return "".join(parts)


def write_tour(out, tour, offers):
    tour_id = first_text(tour, "tour_id")
    acc = tour.find("accommodation")
    title = first_text(acc, "title") if acc is not None else ""
    if not tour_id or not title:
        return False

    w = out.write
    w("<tour>")
    w(f"<tour_id>{escape(tour_id)}</tour_id>")
    w(f"<tour_type>{escape(first_text(tour, 'tour_type'))}</tour_type>")
    w(f"<tour_title>{escape(first_text(tour, 'tour_title'))}</tour_title>")
    w(f"<tour_detail_url>{escape(first_text(tour, 'tour_detail_url'))}</tour_detail_url>")

    loc = tour.find("locations/location")
    w("<locations><location>")
    if loc is not None:
        w(f"<country_id>{escape(first_text(loc, 'country_id'))}</country_id>")
        w(f"<area_title>{escape(first_text(loc, 'area_title'))}</area_title>")
        w(f"<place_title>{escape(first_text(loc, 'place_title'))}</place_title>")
    w("</location></locations>")

    w("<accommodation>")
    w(f"<title>{escape(title)}</title>")
    lat, lon = first_text(acc, "gps/lat"), first_text(acc, "gps/lon")
    if lat and lon:
        w(f"<gps><lat>{escape(lat)}</lat><lon>{escape(lon)}</lon></gps>")
    # popis: spojit obsahové sekce do odstavců (\n\n — šablona z nich dělá <p>)
    sections, all_texts = {}, []
    for d in (acc.iter("accommodation_desc") if acc is not None else []):
        title = (d.get("title") or "").strip()
        text = (d.text or "").strip()
        if text:
            all_texts.append(text)
        if len(text) >= DESC_MIN_SECTION and title not in sections:
            sections[title] = text
    parts = [sections[t] for t in DESC_SECTIONS if t in sections]
    if not parts and all_texts:  # fallback: nejdelší text, i krátký
        parts = [max(all_texts, key=len)]
    desc = "\n\n".join(parts)
    if len(desc) > DESC_MAX_CHARS:
        cut = desc.rfind("\n\n", 0, DESC_MAX_CHARS)
        desc = desc[:cut] if cut > 1000 else desc[:DESC_MAX_CHARS]
    w(f"<desc>{escape(desc)}</desc>")
    w("</accommodation>")

    w("<photos>")
    for i, photo in enumerate(tour.iter("photo")):
        if i >= MAX_PHOTOS:
            break
        url = first_text(photo, "url")
        if url:
            w(f"<photo><url>{escape(url)}</url></photo>")
    w("</photos>")

    # ploché <terms> — offer_item pak je "terms/term"
    w(terms_xml(tour_id, offers))
    w("</tour>\n")
    return True


def stream_tours(env, remote_file, limit_mb=0):
    """Generátor <tour> elementů ze SFTP souboru (curl | iterparse, nic se neukládá celé)."""
    src = f"sftp://{env['CEDOK_SFTP_HOST']}/{remote_file}"
    cmd = ["curl", "-s", "-k", "--connect-timeout", "30",
           "-u", f"{env['CEDOK_SFTP_USER']}:{env['CEDOK_SFTP_PASS']}"]
    if limit_mb:
        cmd += ["--range", f"0-{limit_mb * 1024 * 1024}"]
    cmd += [src, "-o", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        for _, el in ET.iterparse(proc.stdout, events=("end",)):
            if el.tag != "tour":
                continue
            yield el
            el.clear()
    except ET.ParseError as e:
        # useknutý stream v test módu (--limit-mb) je očekávaný
        if not limit_mb:
            raise
        print(f"  (parse konec na useknutém streamu: {e})")
    finally:
        proc.stdout.close()
        proc.wait()


def head_xml(tour):
    """Část <tour> před <terms> (hotel, popis, fotky) jako text."""
    buf = io.StringIO()
    if not write_tour(buf, tour, []):
        return None
    return buf.getvalue()[:-len("<terms></terms></tour>\n")]


def load_far():
    try:
        d = json.load(open(FAR_CACHE, encoding="utf-8"))
        age_h = (datetime.datetime.now().timestamp() - os.path.getmtime(FAR_CACHE)) / 3600
        return d, age_h
    except (OSError, ValueError):
        return None, 1e9


def generate(env, out_path, limit_mb=0, fast=False):
    """Slije dva zdroje Čedoku.

    last-minute soubor (obnovuje se několikrát denně) je autoritou pro odjezdy
    do jeho posledního dne (`window`): u hotelů, které v něm jsou, se bere pouze
    on (termíny, které v něm chybí, jsou vyprodané/přecenené). Hotely, které v něm
    nejsou, a odjezdy za oknem se berou z velkého souboru (denně). Ověřeno
    11. 10. 2026: u 41 % shodných termínů se ceny v souborech liší a velký soubor
    byl u nejbližších odjezdů zastaralý (ceny u nás stály od 14. 9.).
    `fast` = bez velkého souboru, vzdálené termíny z mezipaměti posledního plného běhu.
    """
    today = datetime.date.today().isoformat()
    lm = {}          # tour_id -> (kandidáti, head)
    window = ""
    n_lm = 0
    for tour in stream_tours(env, LM_FILE, limit_mb):
        n_lm += 1
        tid = first_text(tour, "tour_id")
        window = max([window] + [first_text(t, "start") for t in tour.iter("term")])
        lm[tid] = (collect_candidates(tour, today), head_xml(tour))
    print(f"LM soubor: {n_lm} hotelů, okno odjezdů do {window}", flush=True)
    if not lm or not window:
        raise RuntimeError("last-minute soubor je prázdný, nic negeneruji")

    far_cache = {}   # tour_id -> {"head": ..., "cands": [...]} pro rychlé běhy
    seen = set()
    tours = written = offers_total = 0

    def emit(out, tid, head, cands):
        nonlocal written, offers_total
        offers = choose_offers(cands)
        if offers and head:
            out.write(head + terms_xml(tid, offers) + "</tour>\n")
            written += 1
            offers_total += len(offers)

    with open(out_path, "w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="utf-8"?>\n<tours>\n<currency_id>1</currency_id>\n')
        if fast:
            far, age_h = load_far()
            if far is None or age_h > 36:
                raise RuntimeError(f"mezipaměť vzdálených termínů chybí/je stará ({age_h:.0f} h) — spusť plný běh")
            for tid, rec in far.items():
                seen.add(tid)
                lm_c, lm_head = lm.get(tid, ({}, None))
                unpack = lambda d: {tuple(k.split("|")): v for k, v in d.items()}  # noqa: E731
                # hotel mimo LM soubor: nejbližší termíny z velkého souboru (uložené při plném běhu)
                near = {} if tid in lm else unpack(rec.get("near", {}))
                emit(out, tid, rec["head"], merge_candidates(lm_c, near, unpack(rec["cands"])))
                far_cache[tid] = rec
        else:
            for tour in stream_tours(env, env[MAIN_FILE_ENV], limit_mb):
                tours += 1
                tid = first_text(tour, "tour_id")
                seen.add(tid)
                far_c = collect_candidates(tour, today, after=window)
                head = head_xml(tour)
                near_c = {} if tid in lm else collect_candidates(tour, today, until=window)
                emit(out, tid, head, merge_candidates(lm[tid][0] if tid in lm else {}, near_c, far_c))
                if head:
                    far_cache[tid] = {"head": head, "cands": {"|".join(k): v for k, v in far_c.items()},
                                      "near": {"|".join(k): v for k, v in near_c.items()}}
                if tours % 500 == 0:
                    print(f"  ...{tours} tours, {written} kept, {offers_total} offers", flush=True)
        # hotely jen v LM souboru (nové nebo mimo velký soubor)
        for tid, (lm_c, lm_head) in lm.items():
            if tid not in seen:
                emit(out, tid, lm_head, lm_c)
        out.write("</tours>\n")
    if not fast and not limit_mb and far_cache:
        os.makedirs(os.path.dirname(FAR_CACHE), exist_ok=True)
        json.dump(far_cache, open(FAR_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    size = os.path.getsize(out_path)
    print(f"HOTOVO ({'fast' if fast else 'plný'}): {written} hotelů, {offers_total} nabídek, {size/1e6:.1f} MB → {out_path}")
    return written


def upload(env, out_path):
    """Nahraje feed do web rootu a OVĚŘÍ, že ho web servíruje (velikost + Last-Modified).

    Past (11. 10. 2026): po migraci hostingu míří FTP na kořen účtu, web běží z FTP_ROOT
    (/domains/kakapo.cz). Starý skript nahrával do /feeds/ v kořeni účtu → „UPLOAD OK",
    ale web servíroval soubor ze 14. 9. a ceny Čedoku měsíc stály."""
    root = env.get("FTP_ROOT", "").rstrip("/")
    remote = f"{root}{REMOTE_PATH}"
    ftp = f"ftp://{env['FTP_HOST']}"
    user = f"{env['FTP_USER']}:{env['FTP_PASSWORD']}"
    subprocess.run(["curl", "-s", "--ftp-create-dirs", "-T", out_path,
                    "--user", user, f"{ftp}{remote}"], check=True)
    # WEDOS FTP dává 640 → web 403; chmod 644 + 755 na složku
    subprocess.run(["curl", "-s", "--user", user, ftp + "/", "-o", "/dev/null",
                    "-Q", f"SITE CHMOD 644 {remote}",
                    "-Q", f"SITE CHMOD 755 {root}/feeds"], check=True)
    print(f"UPLOAD → {ftp}{remote}")

    # CF purge, ať plugin stáhne čerstvý soubor
    purge = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{env['CF_ZONE_ID']}/purge_cache",
        data=('{"files":["' + FEED_URL + '"]}').encode(),
        headers={"X-Auth-Email": env["CF_AUTH_EMAIL"],
                 "X-Auth-Key": env["CF_GLOBAL_API_KEY"],
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(purge, timeout=30) as r:
        print("CF purge:", r.status)

    # ověření: servírovaný soubor musí mít velikost nahraného a čerstvé Last-Modified
    want = os.path.getsize(out_path)
    last = None
    for attempt in range(8):  # WEDOS propisuje s latencí
        # CF/WEDOS neposílají Content-Length (chunked) a Range ignorují → stáhnout a spočítat bajty
        get = urllib.request.Request(FEED_URL + f"?v={int(datetime.datetime.now().timestamp())}",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
        with urllib.request.urlopen(get, timeout=120) as r:
            last = r.headers.get("Last-Modified")
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
        if got == want:
            print(f"VERIFY OK: web servíruje {got/1e6:.1f} MB, Last-Modified {last}")
            return
        print(f"VERIFY čeká: web {got} B != nahráno {want} B (pokus {attempt + 1})", flush=True)
        import time
        time.sleep(30)
    raise RuntimeError(f"feed na webu neodpovídá nahranému souboru (web {got} B, nahráno {want} B, Last-Modified {last}) — import neběží nad čerstvými daty")


def upload_r2(env, out_path):
    """Astro/Workers varianta: PUT slim feed do R2 inboxu import Workeru (feed #41) + spuštění importu.
    Env: KAKAPO_IMPORT_URL (např. https://kakapo-import-staging.huy-kenny.workers.dev), KAKAPO_IMPORT_SECRET."""
    url = env["KAKAPO_IMPORT_URL"].rstrip("/")
    hdr = {"Authorization": "Bearer " + env["KAKAPO_IMPORT_SECRET"], "User-Agent": "Mozilla/5.0 kakapo-cedok-upload"}
    data = open(out_path, "rb").read()
    req = urllib.request.Request(f"{url}/feeds/41/snapshot", data=data, method="PUT",
                                 headers={**hdr, "Content-Type": "application/xml", "Content-Length": str(len(data))})
    with urllib.request.urlopen(req, timeout=600) as r:
        print(f"R2 UPLOAD → HTTP {r.status} ({len(data)/1e6:.1f} MB)")
    run = urllib.request.Request(f"{url}/feeds/41/run?now=1", method="POST", headers=hdr)
    with urllib.request.urlopen(run, timeout=60) as r:
        print(f"IMPORT QUEUED (workers): HTTP {r.status}")


def queue_import(env, slug="cedok"):
    """Po uploadu zafrontí import feedu přes REST (import/run/{id}),
    ať čerstvá data tečou hned — wp-cron pingy z Maliny už běží à 5 min."""
    import base64
    auth = base64.b64encode(
        f"{env['WP_USER']}:{env['WP_APP_PASSWORD']}".encode()).decode()
    headers = {"Authorization": "Basic " + auth,
               "User-Agent": "MalinaCron/1.0 (kakapo cedok feed)"}
    req = urllib.request.Request(env["WP_URL"] + "/wp-json/kakapo/v1/feeds", headers=headers)
    feeds = __import__("json").load(urllib.request.urlopen(req, timeout=30))
    rows = feeds if isinstance(feeds, list) else feeds.get("feeds", [])
    fid = next((f["id"] for f in rows if f.get("partner_slug") == slug), None)
    if not fid:
        print(f"queue_import: feed '{slug}' nenalezen", file=sys.stderr)
        return
    req = urllib.request.Request(env["WP_URL"] + f"/wp-json/kakapo/v1/import/run/{fid}",
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"IMPORT QUEUED: feed #{fid} → HTTP {r.status}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-mb", type=int, default=0, help="test: číst jen prvních N MB")
    ap.add_argument("--fast", action="store_true", help="jen last-minute soubor + mezipaměť vzdálených termínů z posledního plného běhu")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--target", choices=("wp", "r2", "both"), default="wp", help="kam feed nahrát: wp (FTP, dnes), r2 (Workers import), both")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()

    env = load_env()
    for k in ("KAKAPO_IMPORT_URL", "KAKAPO_IMPORT_SECRET"):  # env var overrides the credentials file (staging vs production)
        if os.environ.get(k):
            env[k] = os.environ[k]
    written = generate(env, args.out, args.limit_mb, fast=args.fast)
    if not args.limit_mb and written < MIN_HOTELS:
        print(f"Podezřele málo hotelů ({written} < {MIN_HOTELS}) — upload přeskočen.", file=sys.stderr)
        sys.exit(1)
    if written == 0:
        print("Žádné hotely — upload přeskočen.", file=sys.stderr)
        sys.exit(1)
    if not args.no_upload:
        if args.target in ("wp", "both"):
            upload(env, args.out)
            try:
                queue_import(env)
            except Exception as e:  # import se stejně spustí sám dle intervalu feedu
                print(f"queue_import selhal (neblokuje): {e}", file=sys.stderr)
        if args.target in ("r2", "both"):
            upload_r2(env, args.out)


if __name__ == "__main__":
    main()
