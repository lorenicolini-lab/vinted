"""Monitor Vinted per orologi: avvisi Telegram su annunci nuovi o con prezzo in calo.

Uso: la ricerca è definita dall'URL che copi dal browser dopo aver impostato i filtri
su vinted.it (categoria, brand, prezzo, condizioni, ...). Va nel secret SEARCH_URL.
Nota: usa endpoint non ufficiali di Vinted, può smettere di funzionare in qualsiasi momento.
"""
import html
import json
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import requests

SEEN_FILE = Path("seen.json")
MAX_SEEN = 5000
DROP_PCT = 0.10  # avvisa di nuovo se il prezzo scende almeno del 10%

# brand: (sotto questa cifra = sospetto fake, sotto questa cifra = possibile affare)
# Soglie di partenza indicative: da tarare in base a quello che vedi davvero.
BRANDS = {
    "seiko": (20, 150),
    "grand seiko": (250, 400),
    "citizen": (15, 100),
    "omega": (150, 400),
    "zenith": (200, 450),
    "longines": (80, 300),
    "bulova": (20, 150),
    "orient": (20, 120),
    "certina": (40, 200),
    "hamilton": (50, 250),
    "vostok": (10, 60),
    "universal genève": (100, 400),
    "universal geneve": (100, 400),
    "tudor": (250, 450),
    "geneva": (10, 30),
}

ESCLUDI = [
    "replica", "copia", "fake", "imitazione", "stile ", "tipo ", "ispirato",
    "non funzionante", "da riparare", "ricambi", "rotto", "guasto",
]
PREMIA = [
    "automatico", "automatic", "carica manuale", "vintage", "anni 60", "anni 70",
    "seamaster", "speedmaster", "constellation", "de ville", "prince", "hi-beat",
    "chronomaster", "el primero", "conquest", "sportsmaster", "king seiko",
    "lordmatic", "sea horse", "khaki", "oyster", "cronografo", "box", "scatola",
]


def sessione():
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "it-IT,it;q=0.9",
    })
    return s


def cerca(s, search_url):
    u = urlparse(search_url)
    base = f"{u.scheme}://{u.netloc}"
    s.get(base, timeout=20)  # ottiene i cookie di sessione
    params = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
              if k not in ("order", "page", "per_page", "time")]
    params += [("order", "newest_first"), ("per_page", "96"), ("page", "1")]
    r = s.get(f"{base}/api/v2/catalog/items", params=params,
              headers={"Accept": "application/json"}, timeout=20)
    if r.status_code in (401, 403, 429):
        print(f"Bloccato da Vinted (HTTP {r.status_code}). Probabile blocco IP.", file=sys.stderr)
        sys.exit(1)
    r.raise_for_status()
    return base, r.json().get("items", [])


def prezzo(item):
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def valuta(item, p):
    """Ritorna (scartare, etichette, punteggio)."""
    titolo = (item.get("title") or "").lower()
    brand = (item.get("brand_title") or "").lower()
    if any(w in titolo for w in ESCLUDI):
        return True, [], 0
    tag, score = [], 0
    soglie = next((v for k, v in BRANDS.items() if k in brand or k in titolo), None)
    if soglie and p is not None:
        fake_sotto, affare_sotto = soglie
        if p < fake_sotto:
            tag.append("⚠️ prezzo sospetto (fake?)")
        elif p <= affare_sotto:
            tag.append("🔥 possibile affare")
            score += 2
    hit = [w for w in PREMIA if w in titolo]
    if hit:
        tag.append("✨ " + ", ".join(hit[:3]))
        score += len(hit)
    return False, tag, score


def telegram(msg):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg, "parse_mode": "HTML",
                            "disable_web_page_preview": "false"}, timeout=20)
    r.raise_for_status()
    time.sleep(1)


def main():
    search_url = os.environ["SEARCH_URL"]
    primo_giro = not SEEN_FILE.exists()
    seen = {} if primo_giro else json.loads(SEEN_FILE.read_text())

    s = sessione()
    time.sleep(random.uniform(1, 4))
    base, items = cerca(s, search_url)
    print(f"{len(items)} annunci ricevuti")

    da_notificare = []
    for it in items:
        iid = str(it.get("id"))
        p = prezzo(it)
        vecchio = seen.get(iid)
        nuovo = vecchio is None
        calo = (not nuovo and p is not None and vecchio is not None
                and p <= vecchio * (1 - DROP_PCT))
        if p is not None and (nuovo or calo):
            seen[iid] = p
        elif nuovo:
            seen[iid] = None
        if not (nuovo or calo):
            continue
        scarta, tag, score = valuta(it, p)
        if scarta:
            continue
        da_notificare.append((score, it, p, tag, calo, vecchio))

    if not primo_giro:
        for score, it, p, tag, calo, vecchio in sorted(da_notificare, key=lambda x: -x[0]):
            user = it.get("user") or {}
            rep = user.get("feedback_reputation")
            righe = [
                ("📉 <b>PREZZO IN CALO</b>" if calo else "🆕 <b>Nuovo annuncio</b>"),
                html.escape(it.get("title") or ""),
                f"💶 {p:.0f} €" + (f" (era {vecchio:.0f} €)" if calo and vecchio else ""),
                f"🏷 {html.escape(it.get('brand_title') or '?')} · {html.escape(str(it.get('status') or ''))}",
            ]
            if rep is not None:
                righe.append(f"⭐ venditore: {float(rep) * 5:.1f}/5")
            righe += tag
            righe.append(it.get("url") or f"{base}/items/{it.get('id')}")
            telegram("\n".join(righe))
        print(f"{len(da_notificare)} avvisi inviati")
    else:
        print("Primo giro: annunci registrati senza notifiche.")

    # tieni solo gli ultimi MAX_SEEN id
    if len(seen) > MAX_SEEN:
        seen = dict(list(seen.items())[-MAX_SEEN:])
    SEEN_FILE.write_text(json.dumps(seen))


if __name__ == "__main__":
    main()
