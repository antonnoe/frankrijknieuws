#!/usr/bin/env python3
import os
import re
import json
import time
import html
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import feedparser
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ------------------------------------------------------------
# CONFIG (pas dit later aan waar nodig)
# ------------------------------------------------------------
REPO_NAME = "frankrijknieuws"

# Dit is de “canonciale” link waar social/nieuwsbrief naartoe wijst.
# Zet dit later op je echte Infofrankrijk permalink.
CHANNEL_LINK = "https://infofrankrijk.com/frankrijk-vandaag/"

CHANNEL_TITLE = "Dagelijkse Frankrijk-update (NL) — Infofrankrijk x Nederlanders.fr"
CHANNEL_DESC  = "Kort, praktisch en actueel nieuws voor Nederlanders met interesse in Frankrijk."

OUTPUT_DAILY_HTML = "frankrijk-vandaag.html"     # pagina-variant (voor WP iframe / archief)
OUTPUT_RSS        = "frankrijk-daily.xml"        # Mailchimp RSS-to-Email
OUTPUT_SIDEBAR    = "sidebar.html"               # Ning 2.0 sidebar iframe
OUTPUT_SOCIAL     = "social.json"                # Zapier/Buffer/Make input
STATE_FILE        = "seen.json"                  # dedupe state (committen)

# Logo's (door jou aangeleverd)
LOGO_INFOFR = "https://lh3.googleusercontent.com/sitesv/APaQ0STE0DcGEJle2BesaxM6XmU752np-4mZvsOK9QUDcR5IPsRPjlQuGXczTDwjDGiNjK75Kl06W7dOyBbHpkp35uWQt4GfbJhYahjc9JA76aRmWMSpkfvylbUv_ty66ctceqWAFWcT8w3x7RAf1_e0ngyamea0t74rgdjwCE-xRLvcdjDmJmU5Z8jEhwTItrT8u7jrIH4BL1lvFToCq3BciqJcwtixWNCya1aauVw=w1280"
LOGO_NLFR   = "https://lh3.googleusercontent.com/sitesv/APaQ0ST5vu1favwXFJYhaSPS3bbPJS0VBmSb7tH024EU-Na517vyo7jY3zY8O-v192RjqljsAQk8UGh2VE2vpcqq7sJsh-dG7gbMoIOCAUCZThwPgDe08EvpxIxmZGf1uc6iWcfOHcPU1SgovMB3Zxp9YJlQCPM4TXBfKAmGyPN9OYkqt6UIBScyHMFotfAk1YFY4k5SFhjtJAo23Zo_TdyCnOqRAOpzZ6GZ72MjmyE=w1280"

BRAND_COLOR = "#800000"

# Nieuwsregels
FRESHNESS_HOURS        = 72   # algemeen nieuws max 72 uur oud
QUIET_DAY_MODE         = "micro"   # 'micro' of 'skip'
MIN_ITEMS_FOR_FULL     = 4
MAX_TOTAL_ITEMS        = 8
PER_RUBRIEK_LIMIT      = 2
SIDEBAR_ITEMS_MAX      = 7

# NLFR: alleen berichten van laatste 24 uur
NLFR_MAX_AGE_HOURS = 24

# Vastgoed (Woningen Aangeboden): max 10 dagen oud; max 3 items per dag
VASTGOED_MAX_AGE_DAYS  = 10
MAX_VASTGOED_PER_DAY   = 3

# Dedupe: geen herhaling binnen 7 dagen
DEDUP_WINDOW_DAYS = 7

PRIORITY_DOMAINS = [
    "infofrankrijk.com",
    "nederlanders.fr",
    "service-public.fr",
    "gouvernement.fr",
    "economie.gouv.fr",
    "insee.fr",
    "meteofrance.com", "meteofrance.fr",
    "bison-fute.gouv.fr",
    "rijksoverheid.nl",
    "nos.nl",
    "nu.nl",
]

PRICE_RE = re.compile(r"€\s?[\d\.\s]+(?:,\d{2})?|€\s?\d[\d\.\s]*", re.I)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def normalize(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "")).strip()

def clean_link(link: str) -> str:
    # verwijder veelvoorkomende tracking
    return re.sub(r"[?&](utm_[^=&]+|fbclid|gclid)=[^&]*", "", link or "")

def entry_id(link: str, title: str) -> str:
    base = (clean_link(link).rstrip("/") or "") + "||" + normalize(title or "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

def parse_date(e) -> datetime:
    if getattr(e, "published_parsed", None):
        ts = time.mktime(e.published_parsed)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if getattr(e, "updated_parsed", None):
        ts = time.mktime(e.updated_parsed)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return now_utc()

def is_fresh(dt: datetime, hours: int) -> bool:
    return (now_utc() - dt) <= timedelta(hours=hours)

def is_new(dt: datetime, hours: int = 72) -> bool:
    return (now_utc() - dt) <= timedelta(hours=hours)

def strip_html(text: str) -> str:
    # simpel, mailvriendelijk
    return re.sub(r"<[^>]+>", "", text or "")

def short(text: str, n: int = 260) -> str:
    t = strip_html(text)
    t = normalize(html.unescape(t))
    return (t[:n] + "…") if len(t) > n else t

def has_price(text: str) -> bool:
    return bool(PRICE_RE.search(text or ""))

def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

def is_nlfr(url: str) -> bool:
    return "nederlanders.fr" in host_of(url)

def is_vastgoed_feed_item(link: str, title: str, desc: str) -> bool:
    # Herkenning: vaak “Woningen Aangeboden” tag feed of woning-keywords
    u = (link or "").lower()
    t = (title + " " + desc).lower()
    return ("wonen" in t and "€" in t) or ("Woningen+Aangeboden".lower() in u) or ("woning" in t and "€" in t)

def rubric(link: str, title: str, desc: str) -> str:
    h = host_of(link)
    txt = (title + " " + desc).lower()

    # Vastgoed komt altijd bovenaan
    if "Woningen+Aangeboden".lower() in (link or "").lower():
        return "vastgoed"

    # NLFR algemeen
    if is_nlfr(link):
        return "community"

    # Weer / veiligheid
    if "meteofrance" in h or "vigilance" in txt or "alerte" in txt:
        return "weer"

    # Reizen / verkeer
    if "bison-fute" in h or "trafic" in txt or "sncf" in txt:
        return "reizen"

    # Praktisch / overheid
    if any(x in h for x in ["service-public", "gouvernement", "economie.gouv", "insee", "infofrankrijk.com"]):
        return "praktisch"

    # Zakelijk
    if any(k in txt for k in ["entreprise", "tva", "micro-entreprise", "inflation", "budget", "impôt", "taux"]):
        return "zakelijk"

    return "actualiteiten"

def score_item(link: str, title: str, desc: str) -> int:
    s = 0
    h = host_of(link)
    if any(d in h for d in PRIORITY_DOMAINS):
        s += 3

    txt = (title + " " + desc).lower()

    # Vastgoed prio
    if "Woningen+Aangeboden".lower() in (link or "").lower():
        s += 6
    elif has_price(txt) and any(k in txt for k in ["maison", "huis", "woning", "villa", "longère", "gîte"]):
        s += 3

    # “actie / deadline” signalen
    if any(k in txt for k in ["décret", "arrêté", "officiel", "échéance", "deadline", "date limite"]):
        s += 2

    # waarschuwingen / verkeer
    if any(k in txt for k in ["vigilance", "alerte", "trafic", "grève", "travaux"]):
        s += 2

    # NL-context klein plusje
    if any(d in h for d in ["nos.nl", "rijksoverheid.nl", "belastingdienst.nl"]):
        s += 1

    return s

def load_feeds(path: str = "feeds.txt"):
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"recent": []}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def rfc2822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


# ------------------------------------------------------------
# Main build
# ------------------------------------------------------------
def main():
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(enabled_extensions=("html",))
    )
    tpl_newsletter = env.get_template("newsletter_template.html")
    tpl_sidebar = env.get_template("sidebar_template.html")
    tpl_static = env.get_template("partials_static_block.html")

    feeds = load_feeds()
    state = load_state()
    recent_guids = {x["guid"] for x in state.get("recent", [])}

    collected = []
    seen_local = set()

    for feed_url in feeds:
        fp = feedparser.parse(feed_url)
        if not fp or not getattr(fp, "entries", None):
            continue

        for e in fp.entries:
            link = e.get("link") or ""
            title = normalize(e.get("title", ""))
            if not link or not title:
                continue

            guid = entry_id(link, title)
            if guid in seen_local or guid in recent_guids:
                continue

            pub = parse_date(e)
            desc = short(e.get("summary", "") or e.get("description", ""))

            rbk = rubric(link, title, desc)

            # Versheidsregels
            if rbk == "vastgoed":
                if (now_utc() - pub) > timedelta(days=VASTGOED_MAX_AGE_DAYS):
                    continue
            elif rbk == "community":
                # NLFR alleen laatste 24 uur
                if (now_utc() - pub) > timedelta(hours=NLFR_MAX_AGE_HOURS):
                    continue
            else:
                if not is_fresh(pub, FRESHNESS_HOURS):
                    continue

            collected.append({
                "guid": guid,
                "link": clean_link(link),
                "title": title,
                "desc": desc,
                "pub": pub,
                "rubriek": rbk,
                "score": score_item(link, title, desc),
                "is_new": is_new(pub, 72),
            })
            seen_local.add(guid)

    # Sortering: score desc, dan recent desc
    collected.sort(key=lambda x: (x["score"], x["pub"]), reverse=True)

    # Selectie met rubriek-limieten
    counts = {}
    selected = []
    for it in collected:
        rbk = it["rubriek"]
        limit = PER_RUBRIEK_LIMIT
        if rbk == "vastgoed":
            limit = MAX_VASTGOED_PER_DAY

        if counts.get(rbk, 0) >= limit:
            continue

        selected.append(it)
        counts[rbk] = counts.get(rbk, 0) + 1

        if len(selected) >= MAX_TOTAL_ITEMS:
            break

    # Quiet day
    if len(selected) < MIN_ITEMS_FOR_FULL:
        if QUIET_DAY_MODE == "skip":
            selected = []
        else:
            # micro: alleen praktisch/weer/reizen/vastgoed + NLFR(24h)
            selected = [x for x in selected if x["rubriek"] in ("praktisch", "weer", "reizen", "vastgoed", "community")]

    # Groeperen in vaste volgorde
    order = ["vastgoed", "praktisch", "weer", "reizen", "zakelijk", "actualiteiten", "community"]
    groups = {k: [] for k in order}
    for it in selected:
        groups[it["rubriek"]].append(it)

    # Datumlabel (NL, zonder locale gedoe)
    dt = now_utc().astimezone(timezone.utc)
    date_label = dt.strftime("%d-%m-%Y")

    # Render daily HTML
    static_block_html = tpl_static.render(brand=BRAND_COLOR)
    daily_html = tpl_newsletter.render(
        brand=BRAND_COLOR,
        logoinfo=LOGO_INFOFR,
        logonlfr=LOGO_NLFR,
        date_label=date_label,
        channel_link=CHANNEL_LINK,
        groups=groups,
        static_block=static_block_html,
    )
    with open(OUTPUT_DAILY_HTML, "w", encoding="utf-8") as f:
        f.write(daily_html)

    # Render sidebar HTML (korte lijst)
    sidebar_items = selected[:SIDEBAR_ITEMS_MAX]
    sidebar_html = tpl_sidebar.render(
        brand=BRAND_COLOR,
        date_label=date_label,
        items=sidebar_items,
        channel_link=CHANNEL_LINK
    )
    with open(OUTPUT_SIDEBAR, "w", encoding="utf-8") as f:
        f.write(sidebar_html)

    # Build RSS (single item) for Mailchimp
    item_title = f"Frankrijk vandaag — {date_label}"
    guid = entry_id(CHANNEL_LINK, item_title)

    rss = []
    rss.append('<?xml version="1.0" encoding="UTF-8"?>')
    rss.append('<rss version="2.0">')
    rss.append("<channel>")
    rss.append(f"<title>{html.escape(CHANNEL_TITLE)}</title>")
    rss.append(f"<link>{html.escape(CHANNEL_LINK)}</link>")
    rss.append(f"<description>{html.escape(CHANNEL_DESC)}</description>")
    rss.append("<language>nl</language>")
    rss.append(f"<lastBuildDate>{rfc2822(now_utc())}</lastBuildDate>")

    rss.append("<item>")
    rss.append(f"<title>{html.escape(item_title)}</title>")
    rss.append(f"<link>{html.escape(CHANNEL_LINK)}</link>")
    rss.append(f"<guid isPermaLink='false'>{guid}</guid>")
    rss.append(f"<pubDate>{rfc2822(now_utc())}</pubDate>")
    rss.append("<description><![CDATA[" + daily_html + "]]></description>")
    rss.append("</item>")

    rss.append("</channel></rss>")

    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write("\n".join(rss))

    # Social JSON (1 post per dag, bullets uit top 3)
    top = selected[:3]
    bullets = [t["title"] for t in top]
    social = {
        "date": date_label,
        "headline": "Frankrijk vandaag (NL) — dagelijkse update",
        "bullets": bullets,
        "link": CHANNEL_LINK
    }
    with open(OUTPUT_SOCIAL, "w", encoding="utf-8") as f:
        json.dump(social, f, ensure_ascii=False, indent=2)

    # Update state (dedupe)
    cutoff = now_utc() - timedelta(days=DEDUP_WINDOW_DAYS)
    kept = []
    for r in state.get("recent", []):
        try:
            d = datetime.fromisoformat(r["pub"])
        except Exception:
            continue
        if d >= cutoff:
            kept.append(r)
    for it in selected:
        kept.append({"guid": it["guid"], "pub": it["pub"].isoformat()})
    state["recent"] = kept
    save_state(state)


if __name__ == "__main__":
    main()
