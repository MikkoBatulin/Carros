"""Coleta diária. Educada: respeita robots.txt, poucas requisições, NÃO contorna bloqueios/CAPTCHA."""
import json, re, time, hashlib, datetime as dt, unicodedata, urllib.robotparser as rp
from urllib.parse import urlparse, urljoin
import requests

UA = "Mozilla/5.0 (compatible; PesquisaCarrosPessoal/1.0; uso pessoal, 1 execucao/dia)"
S = requests.Session(); S.headers["User-Agent"] = UA
CFG = json.load(open("config.json", encoding="utf-8"))
try: DB = json.load(open("data/listings.json", encoding="utf-8"))
except Exception: DB = {"items": {}}
ITEMS = DB.setdefault("items", {})
AGORA = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3)))
HOJE = AGORA.date().isoformat()

class Bloqueado(Exception): pass
_robots = {}
LAST = {}

def permitido(url):
    host = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if host not in _robots:
        r = rp.RobotFileParser(host + "/robots.txt")
        try: r.read()
        except Exception: r = False
        _robots[host] = r
    r = _robots[host]
    return False if r is False else r.can_fetch(UA, url)

def baixar(url):
    if not permitido(url): raise Bloqueado("robots.txt não permite este endereço")
    time.sleep(CFG["pausa_segundos"])
    r = S.get(url, timeout=30)
    LAST["status"] = r.status_code
    if r.status_code in (401, 403, 429) or "captcha" in r.text[:5000].lower():
        raise Bloqueado(f"site bloqueou acesso automatizado (HTTP {r.status_code})")
    r.raise_for_status()
    return r.text

def blobs(html):
    for m in re.finditer(r'<script[^>]*(?:id="__NEXT_DATA__"|type="application/ld\+json")[^>]*>(.*?)</script>', html, re.S):
        try: yield json.loads(m.group(1))
        except Exception: pass

def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values(): yield from walk(v)
    elif isinstance(o, list):
        for v in o: yield from walk(v)

def pick(d, keys):
    low = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, "", [], {}): return v

def tx(v):
    if isinstance(v, dict):
        lv = {str(k).lower(): x for k, x in v.items()}
        v = lv.get("name") or lv.get("value") or lv.get("url") or lv.get("src") or lv.get("photopath") or ""
    if isinstance(v, list): return tx(v[0]) if v else ""
    return "" if v is None else str(v)

def num(v):
    v = tx(v).replace("R$", "").strip()
    if not v: return None
    if re.fullmatch(r"\d+\.\d{1,2}", v): return int(float(v))
    d = re.sub(r"\D", "", v)
    return int(d) if d else None

def norm(s): return re.sub(r"\s+", " ", unicodedata.normalize("NFD", tx(s)).encode("ascii", "ignore").decode().lower()).strip()

def achata(d, prof=2):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.setdefault(k, v)
            if prof > 1:
                for kk, vv in achata(v, prof - 1).items(): out.setdefault(kk, vv)
        elif not isinstance(v, list): out.setdefault(k, v)
    return out

def normaliza(d, base):
    d = {**achata(d), **d}
    o = d.get("offers"); o = o[0] if isinstance(o, list) and o else o
    if isinstance(o, dict): d = {**d, "price": o.get("price"), "url": d.get("url") or o.get("url")}
    preco, url = num(pick(d, ["price", "preco", "valor"])), tx(pick(d, ["url", "link", "permalink", "href"]))
    ano = num(pick(d, ["vehicleModelDate", "modelYear", "yearModel", "anoModelo", "year", "ano"]))
    km = num(pick(d, ["mileageFromOdometer", "mileage", "odometer", "odometro", "km", "quilometragem", "kilometragem"]))
    if not preco or preco < 1000 or not url or (ano is None and km is None): return None
    if ano and not 1990 <= ano <= 2030: ano = None
    loc = tx(pick(d, ["city", "cidade", "addressLocality", "municipio", "location"]))
    uf = tx(pick(d, ["state", "uf", "estado", "addressRegion"]))
    m = re.match(r"(.+?)\s*[-,/]\s*([A-Za-z]{2})$", loc)
    if m: loc, uf = m.group(1), uf or m.group(2)
    if re.search(r"\bmg\b|minas", norm(uf)): uf = "MG"
    blob = json.dumps(d, ensure_ascii=False).lower()
    stp = norm(pick(d, ["sellerType", "tipoVendedor", "advertiserType", "tipoAnunciante"]))
    vend = "pf" if stp in ("pf", "particular", "pessoa fisica", "private") else "pj" if stp in ("pj", "lojista", "loja", "pessoa juridica", "concessionaria", "dealer") else None
    vend = vend or ("pj" if re.search(r"lojista|pessoa jur[ií]dica|professional\W+true|revenda|concession", blob) else
                    "pf" if re.search(r"particular|pessoa f[ií]sica", blob) else None)
    return {"url": urljoin(base, url), "titulo": tx(pick(d, ["name", "title", "titulo"])), "marca": tx(pick(d, ["brand", "marca", "make"])),
            "modelo": tx(pick(d, ["model", "modelo"])), "versao": tx(pick(d, ["version", "versao", "trim"])), "ano": ano, "preco": preco, "km": km,
            "cambio": cambio(tx(pick(d, ["transmission", "cambio", "gearbox", "vehicleTransmission"])) or blob), "cidade": loc.strip(), "uf": uf.strip().upper()[:2] if len(uf.strip()) <= 2 else uf.strip(),
            "vendedor": vend, "unico_dono": True if re.search(r"[uú]nico dono", blob) else None,
            "foto": tx(pick(d, ["image", "photo", "foto", "thumbnail", "images"])), "_blob": blob}

def cambio(t):
    t = norm(t)
    if "automatizad" in t: return "automatizado"
    if re.search(r"automatic|cvt|dct|tiptronic|steptronic", t): return "automatico"
    return "manual" if "manual" in t else None

def negado(t, ini): return re.search(r"\b(sem|nunca|nao|nenhum\w*|isento)\b", t[max(0, ini - 45):ini]) is not None

def sinais(texto):
    """Devolve (leilao, unico_dono, pessoa_fisica). True/False só quando o texto afirma; None = não informado."""
    t = norm(texto)
    leilao = None
    for p in CFG["palavras_leilao"]:
        for m in re.finditer(re.escape(norm(p)), t):
            if negado(t, m.start()): leilao = False if leilao is None else leilao
            else: return True, None, None
    dono = True if any(re.search(p, t) and not negado(t, re.search(p, t).start()) for p in [r"unico dono", r"1 dono", r"um unico proprietario", r"unico proprietario"]) else None
    pf = "pf" if re.search(r"vendedor particular|anunciante particular|pessoa fisica", t) else None
    return leilao, dono, pf

def texto_pagina(html): return re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I))

def detalhar(it):
    html = baixar(it["url"]); alvo = urlparse(it["url"]).path.rstrip("/")
    for b in blobs(html):
        for d in walk(b):
            n = normaliza(d, it["url"])
            if n and urlparse(n["url"]).path.rstrip("/") == alvo:  # detalhe prevalece sobre o resumo
                it.update({k: v for k, v in n.items() if v not in (None, "") and k != "url"})
    leilao, dono, pf = sinais(texto_pagina(html) + " " + it["titulo"])
    it["leilao"] = leilao
    it["unico_dono"] = it.get("unico_dono") or dono
    it["vendedor"] = it.get("vendedor") or pf
    it["detalhado"] = True

def listas(o, cam="", saida=None):
    saida = [] if saida is None else saida
    if isinstance(o, list):
        if len(o) >= 5 and all(isinstance(x, dict) for x in o[:5]): saida.append((len(o), cam, o[0]))
        for i, v in enumerate(o[:3]): listas(v, f"{cam}[{i}]", saida)
    elif isinstance(o, dict):
        for k, v in o.items(): listas(v, f"{cam}.{k}" if cam else k, saida)
    return saida

def caminhos(o, p="", out=None, lim=22):
    out = {} if out is None else out
    if len(out) >= lim: return out
    if isinstance(o, dict):
        for k, v in o.items(): caminhos(v, f"{p}.{k}" if p else k, out, lim)
    elif isinstance(o, list):
        for i, v in enumerate(o[:2]): caminhos(v, f"{p}[{i}]", out, lim)
    else: out[p] = str(o)[:70]
    return out

def diagnostico(html):
    """Resumo técnico do que o site devolveu (curto, para caber num print)."""
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    d = {"http": LAST.get("status"), "bytes": len(html), "titulo": t.group(1).strip()[:80] if t else "",
         "R$": html.count("R$"), "reconhecidos": sum(1 for b in blobs(html) for x in walk(b) if normaliza(x, "https://x/"))}
    for b in blobs(html):
        if isinstance(b, dict) and "props" in b:
            ls = sorted(listas(b), key=lambda t: -t[0])
            dh = [t for t in ls if "dehydratedState" in t[1]][:3]
            d["listas"] = [f"{n} itens: {c[-70:]}" for n, c, _ in ls[:5]]
            d["amostras"] = [{"lista": c[-45:], "item": caminhos(x)} for n, c, x in dh]
    return d

def prelim(it):
    c = CFG["coleta"]
    return (it["preco"] <= c["preco_max"] and (it["km"] is None or it["km"] <= c["km_max"]) and (it["ano"] is None or it["ano"] >= c["ano_min"])
            and it["uf"] in ("", "MG", "Minas Gerais") and it["cambio"] != "manual")

def main():
    status, vistos = {}, {}
    for site, f in CFG["fontes"].items():
        if not f["ativa"]: continue
        st = status[site] = {"ok": True, "msg": "", "n": 0}; achados = {}; amostra = None
        try:
            erro = ""
            for base in f["urls"]:
                try:
                    for p in range(1, f["paginas"] + 1):
                        url = base if p == 1 else f"{base}{'&' if '?' in base else '?'}{f['param_pagina']}={p}"
                        html = baixar(url)
                        if p == 1: st.setdefault("diag", {})[base] = diagnostico(html); amostra = amostra or html[:30000]
                        for b in blobs(html):
                            for d in walk(b):
                                it = normaliza(d, url)
                                if it and prelim(it): achados[it["url"]] = it
                except Exception as e:
                    erro = str(e); st.setdefault("diag", {})[base] = {"erro": erro}
            st["n"] = len(achados)
            if not achados: st.update(ok=False, msg=erro or "nenhum anúncio reconhecido (layout pode ter mudado)")
        except Bloqueado as e: st.update(ok=False, msg=str(e))
        except Exception as e: st.update(ok=False, msg=f"erro: {e}")
        if amostra and not achados: open(f"data/debug_{site}.html", "w", encoding="utf-8").write(amostra)
        limite = CFG["max_detalhes_por_site"]
        for it in achados.values():
            it["site"] = site
            it["id"] = hashlib.sha1(f"{norm(it['marca'])}{norm(it['modelo'] or it['titulo'])}|{it['ano']}|{it['km']}|{norm(it['cidade'])}".encode()).hexdigest()[:12]
            velho = ITEMS.get(it["id"])
            if velho and velho.get("detalhado"): it.update({k: velho[k] for k in ("leilao", "unico_dono", "vendedor", "cambio", "detalhado") if k in velho})
            elif limite > 0:
                limite -= 1
                try: detalhar(it)
                except Bloqueado as e: st.update(ok=False, msg=str(e))
                except Exception as e: st["msg"] = f"falha em alguns detalhes: {e}"
            it.pop("_blob", None); vistos[it["id"]] = it

    for i, it in vistos.items():
        v = ITEMS.get(i)
        if not v:
            ITEMS[i] = {**it, "primeira_vez": HOJE, "ultima_vez": HOJE, "ativo": True, "ausente": 0, "fontes": [{"site": it["site"], "url": it["url"]}]}
        else:
            if it["preco"] < v["preco"]: v["preco_anterior"], v["reduzido_em"] = v["preco"], HOJE
            v.update({k: x for k, x in it.items() if x not in (None, "")}); v.update(ultima_vez=HOJE, ativo=True, ausente=0)
            if not any(f["site"] == it["site"] for f in v["fontes"]): v["fontes"].append({"site": it["site"], "url": it["url"]})
    for i, v in list(ITEMS.items()):
        if i in vistos: continue
        if all(status.get(f["site"], {}).get("ok") for f in v["fontes"]):
            v["ausente"] += 1
            if v["ausente"] >= 2 and v["ativo"]: v["ativo"], v["saiu_em"] = False, HOJE
        if not v["ativo"] and (AGORA.date() - dt.date.fromisoformat(v["saiu_em"])).days > 30: del ITEMS[i]
    DB.update(atualizado=AGORA.isoformat(timespec="minutes"), fontes=status)
    json.dump(DB, open("data/listings.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

if __name__ == "__main__": main()
