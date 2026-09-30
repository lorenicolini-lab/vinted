"""Monitor Vinted per orologi: avvisi Telegram su annunci nuovi o con prezzo in calo.

Uso: la ricerca è definita dall'URL che copi dal browser dopo aver impostato i filtri
su vinted.it (categoria, brand, prezzo, condizioni, ...). Va nel secret SEARCH_URL.
Nota: usa endpoint non ufficiali di Vinted, può smettere di funzionare in qualsiasi momento.
"""
import html
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import requests

SEEN_FILE = Path("seen.json")
MAX_SEEN = 5000
DROP_PCT = 0.10  # avvisa di nuovo se il prezzo scende almeno del 10%
SCARTA_SENZA_RECENSIONI = False  # True = non avvisare per venditori con 0 recensioni (altrimenti solo ⚠️)
SCARTA_ARMADIO_VUOTO = False     # True = non avvisare se l'armadio del venditore risulta vuoto

# brand: (sotto questa cifra = sospetto fake, sotto questa cifra = possibile affare)
# Soglie di partenza indicative: da tarare in base a quello che vedi davvero.
# Paesi da cui accetti annunci (codici a 2 lettere). Lista vuota [] = nessun filtro.
# Esempi: "GB" Regno Unito, "CH" Svizzera, "PL" Polonia, "US" Stati Uniti.
SCARTA_PAESE_SCONOSCIUTO = False  # True = scarta anche gli annunci di cui non si legge il Paese
PAESI_AMMESSI = ["IT", "FR", "DE", "ES", "PT", "NL", "BE", "LU", "AT"]

NOMI_PAESI = {
    "IT": "Italia", "FR": "Francia", "DE": "Germania", "ES": "Spagna", "PT": "Portogallo",
    "NL": "Paesi Bassi", "BE": "Belgio", "LU": "Lussemburgo", "AT": "Austria", "PL": "Polonia",
    "CZ": "Rep. Ceca", "SK": "Slovacchia", "HU": "Ungheria", "RO": "Romania", "BG": "Bulgaria",
    "GR": "Grecia", "HR": "Croazia", "SI": "Slovenia", "LT": "Lituania", "LV": "Lettonia",
    "EE": "Estonia", "FI": "Finlandia", "SE": "Svezia", "DK": "Danimarca", "IE": "Irlanda",
    "GB": "Regno Unito", "CH": "Svizzera", "US": "Stati Uniti", "MT": "Malta", "CY": "Cipro",
}
_EN = {"italy": "IT", "france": "FR", "germany": "DE", "spain": "ES", "portugal": "PT",
       "netherlands": "NL", "belgium": "BE", "luxembourg": "LU", "austria": "AT",
       "poland": "PL", "united kingdom": "GB", "switzerland": "CH", "united states": "US",
       "usa": "US", "stati uniti": "US", "germania": "DE", "francia": "FR", "spagna": "ES"}
NOME_A_COD = {**{v.lower(): k for k, v in NOMI_PAESI.items()}, **_EN}

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


FILTRI = {  # parametro dell'URL del sito -> nome nella nuova API (attribute_ids[...])
    "catalog[]": "catalog", "catalog_ids[]": "catalog", "catalog_ids": "catalog",
    "brand_ids[]": "brand", "brand_ids": "brand",
    "size_ids[]": "size", "size_ids": "size",
    "status_ids[]": "status", "status_ids": "status",
}
PASSA = ("price_from", "price_to", "currency", "search_text")


def cerca(s, search_url):
    u = urlparse(search_url)
    base = f"{u.scheme}://{u.netloc}"
    api = f"{u.scheme}://{u.netloc.replace('www.', 'api.', 1)}"

    # Sessione anonima dal sito: cookie access_token_web + header X-Anon-Id (+ CSRF se presente)
    r0 = s.get(base, timeout=20)
    headers = {"Accept": "application/json", "Origin": base, "Referer": base + "/"}
    if r0.headers.get("X-Anon-Id"):
        headers["X-Anon-Id"] = r0.headers["X-Anon-Id"]
    m = (re.search(r'name=["\']csrf-token["\'] content=["\']([^"\']+)', r0.text)
         or re.search(r'"CSRF_TOKEN"\s*:\s*"([^"]+)"', r0.text, re.I))
    if m:
        headers["X-Csrf-Token"] = m.group(1)

    attr, extra = {}, []
    for k, v in parse_qsl(u.query, keep_blank_values=False):
        if k in FILTRI:
            attr.setdefault(FILTRI[k], []).append(v)
        elif k in PASSA:
            extra.append((k, v))
    extra += [("order", "newest_first"), ("per_page", "96"), ("page", "1")]

    def richiesta(virgole):
        params = list(extra)
        for nome, vals in attr.items():
            if virgole:
                params.append((f"attribute_ids[{nome}]", ",".join(vals)))
            else:
                params += [(f"attribute_ids[{nome}]", v) for v in vals]
        return s.get(f"{api}/svc-catalogue/items", params=params, headers=headers, timeout=20)

    r = richiesta(virgole=False)
    if r.status_code in (400, 422):  # formato dei filtri diverso: riprova con valori separati da virgola
        r = richiesta(virgole=True)
    if r.status_code in (401, 403, 429):
        print(f"Bloccato da Vinted (HTTP {r.status_code}). Probabile blocco IP o sessione rifiutata.",
              file=sys.stderr)
        sys.exit(1)
    if not r.ok:
        print(f"Errore HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        sys.exit(1)
    dati = r.json()
    items = dati.get("items") or dati.get("catalog_items") or []
    if items:
        print("Campi del primo annuncio:", sorted(items[0].keys()))
    else:
        print("Nessun annuncio. Chiavi risposta:", sorted(dati.keys()))
    return base, items, headers


def brand_di(item):
    return (item.get("brand_title") or (item.get("item_box") or {}).get("first_line") or "")


def stato_di(item):
    return (item.get("status") or (item.get("item_box") or {}).get("second_line") or "")


def link_di(item, base):
    """Link completo e cliccabile all'annuncio, anche se l'API restituisce un percorso relativo o nulla."""
    url = item.get("url") or ""
    if url.startswith("/"):
        url = base + url
    if not url.startswith("http"):
        url = f"{base}/items/{item.get('id')}"
    return url


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
    brand = brand_di(item).lower()
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


WATCH_RE = re.compile(
    "orolog|watch|cronograf|chronograph|" + "|".join(re.escape(k) for k in BRANDS), re.I)


def foto_di(item):
    ph = item.get("photo")
    if isinstance(ph, dict):
        return ph.get("full_size_url") or ph.get("url")
    phs = item.get("photos")
    if isinstance(phs, list) and phs and isinstance(phs[0], dict):
        return phs[0].get("full_size_url") or phs[0].get("url")
    return None


def armadio(s, base, headers, user_id, catalogo, cache):
    """(articoli totali, orologi trovati, elenco parziale?) oppure None se non disponibile."""
    if not user_id:
        return None
    if user_id in cache:
        return cache[user_id]
    res = None
    try:
        time.sleep(random.uniform(0.5, 1.5))
        r = s.get(f"{base}/api/v2/wardrobe/{user_id}/items",
                  params={"page": 1, "per_page": 96}, headers=headers, timeout=20)
        if r.ok:
            d = r.json()
            its = d.get("items") or []
            tot = (d.get("pagination") or {}).get("total_entries", len(its))
            n = sum(1 for x in its
                    if (catalogo and str(x.get("catalog_id")) == catalogo)
                    or WATCH_RE.search(x.get("title") or "")
                    or WATCH_RE.search(x.get("brand_title") or ""))
            res = (tot, n, tot > len(its))
        else:
            print(f"Armadio venditore {user_id}: HTTP {r.status_code}")
    except Exception as e:  # dato accessorio: mai bloccare l'avviso
        print(f"Armadio venditore {user_id} non leggibile: {e}")
    cache[user_id] = res
    return res


def trova_paese(obj):
    """Cerca in un JSON (anche annidato) qualsiasi chiave che contenga 'country'. Ritorna (codice, nome)."""
    ris = {"cod": None, "nome": None}

    def walk(o, depth):
        if depth > 4:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if "country" in kl:
                    if isinstance(v, dict):
                        ris["cod"] = ris["cod"] or v.get("iso_code") or v.get("code") or v.get("iso")
                        ris["nome"] = ris["nome"] or v.get("title") or v.get("name")
                    elif isinstance(v, str) and v.strip():
                        if "iso" in kl or "code" in kl:
                            ris["cod"] = ris["cod"] or v
                        else:
                            ris["nome"] = ris["nome"] or v
                elif isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for x in o[:20]:
                walk(x, depth + 1)

    walk(obj, 0)
    return ris["cod"], ris["nome"]


def profilo_venditore(s, base, headers, user_id, cache, diag):
    """(codice, nome paese, città) dal profilo del venditore; None se non disponibile."""
    if not user_id:
        return None
    if user_id in cache:
        return cache[user_id]
    res = None
    try:
        time.sleep(random.uniform(0.3, 1.0))
        r = s.get(f"{base}/api/v2/users/{user_id}", headers=headers, timeout=20)
        if r.ok:
            j = r.json()
            u = j.get("user") or j
            if not diag:
                print("Campi profilo venditore:", sorted(u.keys()))
                diag.append(1)
            cod, nome = trova_paese(u)
            res = (cod, nome, u.get("city"))
        else:
            print(f"Profilo venditore {user_id}: HTTP {r.status_code}")
    except Exception as e:
        print(f"Profilo venditore {user_id} non leggibile: {e}")
    cache[user_id] = res
    return res


def dettaglio(s, base, headers, item_id, diag):
    """Stato dell'annuncio e recensioni del venditore. None se il dato non è disponibile."""
    try:
        time.sleep(random.uniform(0.3, 1.0))
        r = s.get(f"{base}/api/v2/items/{item_id}", headers=headers, timeout=20)
        if not r.ok:
            print(f"Dettaglio {item_id}: HTTP {r.status_code}")
            return None
        j = r.json()
        d = j.get("item") or j
    except Exception as e:  # dato accessorio: mai bloccare l'avviso
        print(f"Dettaglio {item_id} non leggibile: {e}")
        return None
    if not diag:
        print("Campi dettaglio:", sorted(d.keys()))
        print("Campi venditore:", sorted((d.get("user") or {}).keys()))
        diag.append(1)
    motivo = None
    if d.get("is_closed"):
        motivo = "venduto/chiuso"
    elif d.get("is_reserved"):
        motivo = "prenotato"
    elif d.get("is_hidden"):
        motivo = "nascosto"
    elif d.get("can_buy") is False:
        motivo = "non acquistabile"
    u = d.get("user") or {}
    fb = u.get("feedback_count")
    if fb is None:
        parti = [u.get(k) for k in ("positive_feedback_count", "neutral_feedback_count",
                                    "negative_feedback_count")]
        if any(x is not None for x in parti):
            fb = sum(x or 0 for x in parti)
    cod, nome = trova_paese(d)
    citta = u.get("city") or d.get("city")
    return {"motivo": motivo, "feedback": fb, "cod": cod, "nome": nome, "citta": citta}


def norm_paese(cod, nome):
    cod = str(cod).strip() if cod else None
    nome = str(nome).strip() if nome else None
    if cod and len(cod) != 2:  # es. "Italy": è un nome, non un codice
        nome, cod = nome or cod, None
    if cod:
        cod = cod.upper()
    elif nome:
        cod = NOME_A_COD.get(nome.lower())
    if cod and not nome:
        nome = NOMI_PAESI.get(cod, cod)
    return cod, nome


def riga_armadio(info):
    if not info:
        return "👜 Armadio venditore: dato non disponibile"
    tot, n, parziale = info
    piu = "+" if parziale else ""
    if n >= 5:
        return (f"👜 Molti orologi nell'armadio: {n}{piu} su {tot} articoli "
                f"(questo incluso) → venditore abituale")
    return (f"👜 Meno di 5 orologi nell'armadio: {n} su {tot} articoli "
            f"(questo incluso) → probabile privato"
            + (" (controllati i primi 96)" if parziale else ""))


def telegram(msg, foto=None):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    api = f"https://api.telegram.org/bot{token}"
    inviato = False
    if foto and len(msg) <= 1024:  # limite Telegram per le didascalie
        r = requests.post(f"{api}/sendPhoto",
                          data={"chat_id": chat, "photo": foto, "caption": msg,
                                "parse_mode": "HTML"}, timeout=30)
        inviato = r.ok
        if not r.ok:
            print(f"sendPhoto fallito ({r.status_code}): {r.text[:200]}")
    if not inviato:
        r = requests.post(f"{api}/sendMessage",
                          data={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
                          timeout=20)
        r.raise_for_status()
    time.sleep(1)


def main():
    search_url = os.environ["SEARCH_URL"]
    primo_giro = not SEEN_FILE.exists()
    seen = {} if primo_giro else json.loads(SEEN_FILE.read_text())

    s = sessione()
    time.sleep(random.uniform(1, 4))
    base, items, headers = cerca(s, search_url)
    catalogo = next((v for k, v in parse_qsl(urlparse(search_url).query)
                     if k in ("catalog[]", "catalog_ids[]", "catalog_ids")), None)
    cache_armadi = {}
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
        if p is None:
            print(f"Annuncio {iid} senza prezzo leggibile, saltato")
            continue
        scarta, tag, score = valuta(it, p)
        if scarta:
            continue
        da_notificare.append((score, it, p, tag, calo, vecchio))

    if not primo_giro:
        diag, inviati = [], 0
        cache_profili, diag_profilo = {}, []
        for score, it, p, tag, calo, vecchio in sorted(da_notificare, key=lambda x: -x[0]):
            user = it.get("user") or {}
            tag = list(tag)

            det = dettaglio(s, base, headers, it.get("id"), diag)
            if det and det["motivo"]:
                print(f"Scartato {it.get('id')}: {det['motivo']}")
                continue
            c1, n1 = trova_paese(user)
            cod, nome_paese = norm_paese((det or {}).get("cod") or c1, (det or {}).get("nome") or n1)
            citta = (det or {}).get("citta") or user.get("city")
            if not cod:  # ultimo tentativo: profilo del venditore
                pv = profilo_venditore(s, base, headers, user.get("id"), cache_profili, diag_profilo)
                if pv:
                    cod, nome_paese = norm_paese(pv[0] or cod, pv[1] or nome_paese)
                    citta = citta or pv[2]
            print(f"Paese annuncio {it.get('id')}: {cod} / {nome_paese} / {citta}")
            if PAESI_AMMESSI and cod and cod not in PAESI_AMMESSI:
                print(f"Scartato {it.get('id')}: paese {cod} non ammesso")
                continue
            if PAESI_AMMESSI and not cod and SCARTA_PAESE_SCONOSCIUTO:
                print(f"Scartato {it.get('id')}: paese non leggibile")
                continue
            fb = det["feedback"] if det and det["feedback"] is not None else user.get("feedback_count")
            if fb == 0:
                if SCARTA_SENZA_RECENSIONI:
                    print(f"Scartato {it.get('id')}: venditore senza recensioni")
                    continue
                tag.append("⚠️ venditore senza recensioni")

            info = armadio(s, base, headers, user.get("id"), catalogo, cache_armadi)
            print(f"Armadio venditore {user.get('id')}: {info}")
            if info and info[0] == 0:
                if SCARTA_ARMADIO_VUOTO:
                    print(f"Scartato {it.get('id')}: armadio vuoto")
                    continue
                tag.append("⚠️ armadio vuoto: annuncio forse chiuso o account sospeso")

            rep = user.get("feedback_reputation")
            righe = [
                ("📉 <b>PREZZO IN CALO</b>" if calo else "🆕 <b>Nuovo annuncio</b>"),
                html.escape(it.get("title") or ""),
                f"💶 {p:.0f} €" + (f" (era {vecchio:.0f} €)" if calo and vecchio else ""),
                f"🏷 {html.escape(brand_di(it) or '?')} · {html.escape(str(stato_di(it)))}",
            ]
            if cod or nome_paese:
                righe.append("🌍 " + html.escape(nome_paese or cod) + (f", {html.escape(str(citta))}" if citta else ""))
            else:
                righe.append("🌍 Paese non disponibile" + (" (filtro paesi non applicato)" if PAESI_AMMESSI else ""))
            if rep is not None:
                righe.append(f"⭐ venditore: {float(rep) * 5:.1f}/5")
            righe += tag
            righe.append(riga_armadio(info))
            righe.append(f'🔗 <a href="{html.escape(link_di(it, base), quote=True)}">Apri su Vinted</a>')
            telegram("\n".join(righe), foto_di(it))
            inviati += 1
        print(f"{inviati} avvisi inviati su {len(da_notificare)} candidati")
    else:
        print("Primo giro: annunci registrati senza notifiche.")

    # tieni solo gli ultimi MAX_SEEN id
    if len(seen) > MAX_SEEN:
        seen = dict(list(seen.items())[-MAX_SEEN:])
    SEEN_FILE.write_text(json.dumps(seen))


if __name__ == "__main__":
    main()
