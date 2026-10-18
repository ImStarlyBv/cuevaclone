"""
cuevana3r_client.py — cliente de recon para cuevana3r.pro

No existe una API JSON. El sitio es SSR: el catalogo y las fuentes de video
vienen embebidos en el HTML. Este cliente parsea esas tres superficies:

  1. Catalogo    GET /peliculas?page=N  |  /series?page=N
  2. Busqueda    GET /explorar?s=<query>&page=N
  3. Detalle     GET /pelicula/<slug>           -> data-server por idioma
                 GET /serie/<slug>/temporada-N  -> lista de episodios
                 GET /serie/<slug>/episodio-NxM -> data-server

Cada `data-server` es una de dos cosas:
  - ...?v=<base64>       -> URL real del proveedor externo (vsembed, vidlink, videasy, vidapi...)
  - ...?token=<opaque>   -> player propio de cuevana (Hyper / Nebula / Goldmember / Death Star),
                            el HTML se sirve y resuelve la fuente por JS en el cliente.

Uso:
    python cuevana3r_client.py catalog 1
    python cuevana3r_client.py search "spider"
    python cuevana3r_client.py movie spider-man-un-nuevo-dia
    python cuevana3r_client.py series la-casa-del-dragon 3
"""

import base64
import json
import re
import sys
import time
from urllib.parse import quote

import requests

BASE = "https://cuevana3r.pro"
PLAYER_HOST = "tungtungsahur.cuevana3r.pro"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

_TAG = re.compile(r"<[^>]+>")


def _clean(s):
    return re.sub(r"\s+", " ", _TAG.sub("", s or "")).strip()


def session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Referer": BASE + "/",
        }
    )
    return s


def get_html(sess, path, retries=3, delay=1.5):
    """GET con reintentos. Devuelve el HTML o lanza la ultima excepcion."""
    url = path if path.startswith("http") else BASE + path
    last = None
    for i in range(retries):
        try:
            r = sess.get(url, timeout=25)
            if r.status_code == 200:
                return r.text
            last = RuntimeError(f"{url} -> HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(delay * (i + 1))
    raise last


# --------------------------------------------------------------------------
# Catalogo y busqueda
# --------------------------------------------------------------------------

_ITEM = re.compile(
    r'<a href="[^"]*/(?P<kind>pelicula|serie)/(?P<slug>[a-z0-9-]+)"[^>]*>'
    r'(?P<body>.*?)</a>',
    re.S,
)
_IMG_ALT = re.compile(r'<img[^>]*\balt="(?:pel[ií]cula|serie)\s+([^"]+)"', re.I)
_YEAR = re.compile(r'class="year[^"]*">(\d{4})<')


def parse_items(html):
    """Extrae (tipo, slug, titulo, anio) de una grilla del catalogo."""
    out, seen = [], set()
    for m in _ITEM.finditer(html):
        slug = m.group("slug")
        if slug in seen:
            continue
        seen.add(slug)
        body = m.group("body")
        alt = _IMG_ALT.search(body)
        year = _YEAR.search(body)
        out.append(
            {
                "kind": m.group("kind"),
                "slug": slug,
                "title": alt.group(1).strip() if alt else None,
                "year": int(year.group(1)) if year else None,
                "url": f"{BASE}/{m.group('kind')}/{slug}",
            }
        )
    return out


def catalog(page=1, kind="peliculas"):
    sess = session()
    return parse_items(get_html(sess, f"/{kind}?page={page}"))


def search(query, page=1):
    sess = session()
    path = f"/explorar?s={quote(query)}"
    if page > 1:
        path += f"&page={page}"
    html = get_html(sess, path)
    return {"query": query, "page": page, "results": parse_items(html)}


# --------------------------------------------------------------------------
# Detalle: metadata + fuentes
# --------------------------------------------------------------------------

_TITLE = re.compile(r"<title>([^<]+)</title>")
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_TMDB_IMG = re.compile(r"https://image\.tmdb\.org/t/p/[^\"'\s>]+")
_LDJSON = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)
_TAB = re.compile(r'<li class="tab-video-item">(.*?)(?=<li class="tab-video-item">|\Z)', re.S)
_FLAG = re.compile(r'/image/(lat|cas|sub)\.png')
_SERVER = re.compile(r'<li[^>]*data-server="([^"]+)"[^>]*>(.*?)</li>', re.S)

LANG = {"lat": "Latino", "cas": "Castellano", "sub": "Subtitulado"}

# --- esquema del player propio (Hyper / Nebula) ---------------------------
# Extraido del <script> inline del shell servido en tungtungsahur.cuevana3r.pro.
# token = <id_servidor:1char> + base64( file_id XOR key )
_TOKEN_KEY = b"a45f04ce-2394-47c3-b718-0ecd97ce51d6"
_TOKEN_SERVERS = {
    "1": "https://lkhjerbhye3wjkhoduh5xiczuvd.lol/v/",
    "2": "https://filemoon.sx/e/",
    "3": "https://lkhjerbhye3wjkhodvh5xlczuvd.lol/e/",
    "4": "https://dood.li/e/",
}


def decode_token(token):
    """token -> (url_final, detalle).  Sin red: puro base64 + XOR."""
    sid = token[0]
    raw = token[1:]
    raw += "=" * (-len(raw) % 4)
    data = base64.b64decode(raw)
    file_id = bytes(b ^ _TOKEN_KEY[i % len(_TOKEN_KEY)] for i, b in enumerate(data)).decode(
        "utf-8", "replace"
    )
    base = _TOKEN_SERVERS.get(sid, f"<servidor {sid} desconocido>")
    return base + file_id, {"server_id": sid, "base": base, "file_id": file_id}


def decode_server(ds_url):
    """Traduce el data-server a su forma util.

    Devuelve {'type': 'embed', 'url': ...}  o  {'type': 'token', 'url': ...}
    """
    if "?v=" in ds_url:
        b64 = ds_url.split("?v=", 1)[1]
        b64 += "=" * (-len(b64) % 4)
        try:
            return {"type": "embed", "url": base64.b64decode(b64).decode("utf-8", "replace")}
        except Exception:  # noqa: BLE001
            return {"type": "unknown", "url": ds_url}
    if "?token=" in ds_url:
        token = ds_url.split("?token=", 1)[1].split("&")[0]
        try:
            resolved, detail = decode_token(token)
            return {"type": "token", "token": token, "url": resolved, **detail}
        except Exception as e:  # noqa: BLE001
            return {"type": "token", "token": token, "url": ds_url, "error": str(e)}
    return {"type": "unknown", "url": ds_url}


def parse_servers(html):
    """Devuelve las fuentes agrupadas por idioma, en orden de aparicion."""
    groups = []
    tabs = _TAB.findall(html)
    # si no hay tabs con flag, caemos a parsear todos los data-server del documento
    blocks = tabs if tabs else [html]
    for block in blocks:
        flag = _FLAG.search(block)
        lang = LANG.get(flag.group(1), "Desconocido") if flag else "Desconocido"
        servers = []
        for url, body in _SERVER.findall(block):
            label = _clean(body).replace("Reproducir", "").strip()
            servers.append({"name": label or None, **decode_server(url)})
        if servers:
            groups.append({"language": lang, "servers": servers})
    return groups


def _ld(html):
    for raw in _LDJSON.findall(html):
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and data.get("@type") in {"Movie", "TVSeries"}:
            return data
    return {}


_YEAR_BLOCK = re.compile(r'Año de estreno</h2>\s*<p>\s*(\d{4})', re.I)
_YEAR_TITLE = re.compile(r"\((\d{4})\)")


def _year(html, ld):
    for getter in (
        lambda: (ld.get("datePublished") or "")[:4],
        lambda: (_YEAR_BLOCK.search(html) or [None, None])[1] if _YEAR_BLOCK.search(html) else "",
        lambda: (_YEAR_TITLE.search(_TITLE.search(html).group(1)) or [None, None])[1]
        if _TITLE.search(html) else "",
    ):
        v = getter()
        if v:
            return int(v)
    return None


def movie(slug):
    sess = session()
    html = get_html(sess, f"/pelicula/{slug}")
    ld = _ld(html)
    h1 = _H1.search(html)
    return {
        "kind": "movie",
        "slug": slug,
        "title": ld.get("name") or (_clean(h1.group(1)) if h1 else None),
        "year": _year(html, ld),
        "tmdb_poster": (_TMDB_IMG.search(html).group(0) if _TMDB_IMG.search(html) else None),
        "sources": parse_servers(html),
    }


_EP = re.compile(r'/serie/([a-z0-9-]+)/episodio-(\d+)x(\d+)')


def series(slug, season=None):
    """Lista los episodios de una temporada (o de todas si season=None)."""
    sess = session()
    seasons = [season] if season else []
    if not seasons:
        html = get_html(sess, f"/serie/{slug}")
        seasons = sorted({int(s) for _, s, _ in _EP.findall(html)}) or [1]
    eps = []
    for s in seasons:
        html = get_html(sess, f"/serie/{slug}/temporada-{s}")
        for _, ss, ee in _EP.findall(html):
            if int(ss) == s:
                eps.append(
                    {
                        "season": int(ss),
                        "episode": int(ee),
                        "url": f"{BASE}/serie/{slug}/episodio-{ss}x{ee}",
                    }
                )
    return {"kind": "series", "slug": slug, "episodes": eps}


def episode(slug, season, ep):
    sess = session()
    html = get_html(sess, f"/serie/{slug}/episodio-{season}x{ep}")
    ld = _ld(html)
    return {
        "kind": "episode",
        "slug": slug,
        "season": season,
        "episode": ep,
        "title": ld.get("name"),
        "sources": parse_servers(html),
    }


# --------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "catalog":
        page = int(argv[2]) if len(argv) > 2 else 1
        kind = argv[3] if len(argv) > 3 else "peliculas"
        print(json.dumps(catalog(page, kind), ensure_ascii=False, indent=1))
    elif cmd == "search":
        print(json.dumps(search(argv[2]), ensure_ascii=False, indent=1))
    elif cmd == "movie":
        print(json.dumps(movie(argv[2]), ensure_ascii=False, indent=1))
    elif cmd == "series":
        season = int(argv[3]) if len(argv) > 3 else None
        print(json.dumps(series(argv[2], season), ensure_ascii=False, indent=1))
    elif cmd == "episode":
        print(json.dumps(episode(argv[2], int(argv[3]), int(argv[4])), ensure_ascii=False, indent=1))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
