#!/usr/bin/env python3
"""kakapo.cz — ATIS (atis.cz, eHub affiliate) slim feed.

Zdroj: SDovolena XML (~890 MB, 513 hotelů × ~2 800 termínů). Worker by to nesnědl, takže
Malina feed streamuje (curl | iterparse), z každého hotelu nechá nejlevnější budoucí termín
pro každou kombinaci strava × délka pobytu (max 12 nabídek) a výsledek (~3 MB) nahraje do
R2 inboxu import Workeru (feed ATIS, adapter `cedok-slim` = stejný slim formát jako Čedok).
eHub deeplink se na odkaz nabídky lepí až v importu (feeds.affiliate_params s {url}).

Použití:  atis-slim-feed.py [--no-upload] [--feed-id 43] (44 = wellness produkty) [--out /tmp/atis-slim.xml]
Env (credentials/.kakapo-credentials.env): ATIS_FEED_URL, KAKAPO_IMPORT_URL, KAKAPO_IMPORT_SECRET.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

ENV_FILE = os.environ.get("KAKAPO_ENV_FILE", "/home/kenny/ai/credentials/.kakapo-credentials.env")
NS = "{http://www.sdovolena.cz}"
MIN_HOTELS = 300       # pojistka: míň = chyba zdroje, nenahrávat
MAX_OFFERS = 12
MAX_PHOTOS = 8
DESC_MAX = 4000
BOARD = {"1": "bez-stravy", "2": "snidane", "3": "bez-stravy", "4": "polopenze", "5": "plna-penze", "6": "all-inclusive"}
TRANSPORT = {"1": "vlastní"}  # ATIS = pobyty vlastní dopravou


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


def t(el, path):
    """Text prvního potomka (s namespace)."""
    if el is None:
        return ""
    node = el.find("/".join(NS + p for p in path.split("/")))
    return (node.text or "").strip() if node is not None and node.text else ""


def stream_tours(url):
    proc = subprocess.Popen(["curl", "-sSL", "--fail", "--connect-timeout", "30", "--max-time", "1800", url, "-o", "-"],
                            stdout=subprocess.PIPE)
    try:
        for _, el in ET.iterparse(proc.stdout, events=("end",)):
            if el.tag == NS + "tour":
                yield el
                el.clear()
    finally:
        proc.stdout.close()
        if proc.wait() != 0:
            raise RuntimeError("stažení ATIS feedu selhalo (curl)")


def candidates(tour, today):
    """Nejlevnější budoucí termín pro (strava, počet nocí) — pobytové balíčky se liší hlavně délkou."""
    best = {}
    for group in tour.iter(NS + "term_group"):
        board = t(group, "board/id")
        for term in group.iter(NS + "term"):
            start = t(term, "start")
            if not start or start < today:
                continue
            try:
                price = int(float(t(term, "prices/price/final_price") or 0))
            except ValueError:
                continue
            url = t(term, "purchase_url")
            if price <= 0 or not url.startswith("http"):
                continue
            nights = t(term, "night_count") or "1"
            key = (board, nights)
            cur = best.get(key)
            if cur is None or (price, start) < (cur["price"], cur["start"]):
                best[key] = {"id": t(term, "id"), "start": start, "end": t(term, "end"), "nights": nights,
                             "price": price, "url": url, "board": board}
    return list(best.values())


def wellness_offers(tour, today):
    """Až 40 nejbližších levných termínů (nejlevnější na datum+noci)."""
    c = sorted(candidates(tour, today), key=lambda o: o["start"])
    return c[:40] or candidates(tour, today)


def pick(cands):
    """Max MAX_OFFERS; nejdřív nejlevnější z každé stravy, pak dolévat podle ceny."""
    cands = sorted(cands, key=lambda o: (o["price"], o["start"]))
    picked, boards = [], set()
    for o in cands:
        if o["board"] not in boards:
            boards.add(o["board"])
            picked.append(o)
    for o in cands:
        if len(picked) >= MAX_OFFERS:
            break
        if o not in picked:
            picked.append(o)
    return sorted(picked[:MAX_OFFERS], key=lambda o: (o["price"], o["start"]))


def clean(text):
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > DESC_MAX:
        cut = text.rfind("\n\n", 0, DESC_MAX)
        text = text[:cut] if cut > 1000 else text[:DESC_MAX]
    return text


def write_tour(out, tour, offers):
    tid, acc = t(tour, "tour_id"), tour.find(NS + "accommodation")
    title = t(acc, "title") or t(tour, "tour_title")
    if not tid or not title:
        return False
    w = out.write
    w(f"<tour><tour_id>{escape(tid)}</tour_id><tour_type>{escape(t(tour, 'tour_type'))}</tour_type>")
    w(f"<tour_title>{escape(t(tour, 'tour_title'))}</tour_title><tour_detail_url>{escape(t(tour, 'tour_detail_url'))}</tour_detail_url>")
    loc = tour.find(f"{NS}locations/{NS}location")
    w("<locations><location>")
    for tag in ("country_id", "area_title", "place_title"):
        w(f"<{tag}>{escape(t(loc, tag))}</{tag}>")
    w("</location></locations>")
    w(f"<accommodation><title>{escape(title)}</title>")
    lat, lon = t(acc, "gps/lat"), t(acc, "gps/lon")
    if lat and lon:
        w(f"<gps><lat>{escape(lat)}</lat><lon>{escape(lon)}</lon></gps>")
    w(f"<desc>{escape(clean(t(tour, 'tour_desc')))}</desc></accommodation><photos>")
    for i, p in enumerate(tour.iter(NS + "photo")):
        if i >= MAX_PHOTOS:
            break
        url = t(p, "url")
        if url:
            w(f"<photo><url>{escape(url)}</url></photo>")
    w("</photos><terms>")
    for o in offers:
        w(f"<term><id>{escape(tid)}-{escape(o['id'])}-b{o['board'] or '0'}</id><start>{o['start']}</start><end>{o['end']}</end>"
          f"<night_count>{o['nights']}</night_count><final_price>{o['price']}</final_price>"
          f"<meal>{BOARD.get(o['board'], 'bez-stravy')}</meal><transport>{TRANSPORT['1']}</transport><airport></airport>"
          f"<purchase_url>{escape(o['url'])}</purchase_url></term>")
    w("</terms></tour>\n")
    return True


WELLNESS = re.compile(r"wellness|\bspa\b|lázn|relax", re.I)


def write_product(out, tour, offers):
    """Wellness hotel jako produkt (kategorie Wellness pobyty) — nabídky = termíny s odkazem na rezervaci."""
    tid, title = t(tour, "tour_id"), t(tour, "tour_title")
    loc = tour.find(f"{NS}locations/{NS}location")
    photos = [t(p, "url") for p in tour.iter(NS + "photo")][:MAX_PHOTOS]
    w = out.write
    w(f'<hotel id="{escape(tid)}"><name>{escape(title)}</name><description>{escape(clean(t(tour, "tour_desc")))}</description>')
    w(f"<url>{escape(offers[0]['url'])}</url><price>{min(o['price'] for o in offers)}</price>")
    w(f"<country>{escape(t(loc, 'country_id'))}</country><locality>{escape(t(loc, 'area_title'))}</locality><place>{escape(t(loc, 'place_title'))}</place><images>")
    for u in photos:
        if u:
            w(f"<image>{escape(u)}</image>")
    w("</images><offers>")
    for o in offers:
        w(f"<offer><date_from>{o['start']}</date_from><date_to>{o['end']}</date_to><nights>{o['nights']}</nights><price>{o['price']}</price><url>{escape(o['url'])}</url></offer>")
    w("</offers></hotel>\n")


def generate(url, out_path, wellness_path=None):
    today = datetime.date.today().isoformat()
    n = written = offers_total = 0
    wf = open(wellness_path, "w", encoding="utf-8") if wellness_path else None
    nw = 0
    if wf:
        wf.write('<?xml version="1.0" encoding="utf-8"?>\n<hotels>\n')
    with open(out_path, "w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="utf-8"?>\n<tours>\n')
        for tour in stream_tours(url):
            n += 1
            offers = pick(candidates(tour, today))
            if offers and write_tour(out, tour, offers):
                written += 1
                offers_total += len(offers)
                if wf and WELLNESS.search(t(tour, "tour_title")):
                    write_product(wf, tour, wellness_offers(tour, today))
                    nw += 1
        out.write("</tours>\n")
    if wf:
        wf.write("</hotels>\n")
        wf.close()
        print(f"wellness produkty: {nw}")
    print(f"ATIS: {n} hotelů ve feedu, {written} s budoucími termíny, {offers_total} nabídek, {os.path.getsize(out_path)/1e6:.1f} MB")
    return written


def upload_r2(env, feed_id, path):
    base = env["KAKAPO_IMPORT_URL"].rstrip("/")
    hdr = {"Authorization": "Bearer " + env["KAKAPO_IMPORT_SECRET"], "User-Agent": "Mozilla/5.0 kakapo-atis-upload"}
    data = open(path, "rb").read()
    req = urllib.request.Request(f"{base}/feeds/{feed_id}/snapshot", data=data, method="PUT",
                                 headers={**hdr, "Content-Type": "application/xml", "Content-Length": str(len(data))})
    with urllib.request.urlopen(req, timeout=600) as r:
        print(f"R2 UPLOAD → HTTP {r.status} ({len(data)/1e6:.1f} MB)")
    with urllib.request.urlopen(urllib.request.Request(f"{base}/feeds/{feed_id}/run?now=1", method="POST", headers=hdr), timeout=60) as r:
        print(f"IMPORT QUEUED: HTTP {r.status}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed-id", type=int, default=43)
    ap.add_argument("--out", default="/tmp/atis-slim.xml")
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()
    env = load_env()
    for k in ("KAKAPO_IMPORT_URL", "KAKAPO_IMPORT_SECRET"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    wpath = args.out.replace(".xml", "-wellness.xml")
    written = generate(env["ATIS_FEED_URL"], args.out, wpath)
    if written < MIN_HOTELS:
        sys.exit(f"Podezřele málo hotelů ({written} < {MIN_HOTELS}) — upload přeskočen.")
    if not args.no_upload:
        upload_r2(env, args.feed_id, args.out)
        upload_r2(env, args.feed_id + 1, wpath)  # feed 44 = ATIS wellness produkty


if __name__ == "__main__":
    main()
