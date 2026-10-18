"""Web pública: SSR con Jinja2, player hls.js y resolución on-demand."""

import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from scraper.client import UA, parse_servers, session
from scraper.resolve import ResolveError, check_master, resolve

from . import db

SITE_NAME = os.environ.get("SITE_NAME", "Cuevana")
SITE_URL = os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/")
ADS_ENABLED = os.environ.get("ADS_ENABLED", "false").lower() == "true"

HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="cuevaclone", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.globals.update(site_name=SITE_NAME, site_url=SITE_URL, ads=ADS_ENABLED)

_sess = None


def http():
    global _sess
    if _sess is None:
        _sess = session()
    return _sess


@app.on_event("startup")
def _startup():
    db.open_pool()
    db.init_schema()


def render(name, request, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def _page(request, key):
    try:
        return max(1, int(request.query_params.get(key, 1)))
    except ValueError:
        return 1


# --- salud ------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    out = {"status": "ok", "db": "ok", "redis": "skip"}
    try:
        row = db.q1("SELECT 1 AS x")
        out["db"] = "ok" if row else "empty"
    except Exception as exc:
        out["status"] = "degraded"
        out["db"] = f"error: {type(exc).__name__}"
    try:
        import redis as _redis

        url = os.environ.get("REDIS_URL")
        if url:
            _redis.from_url(url, socket_connect_timeout=2).ping()
            out["redis"] = "ok"
    except Exception:
        out["redis"] = "down"
    code = 200 if out["status"] == "ok" else 503
    return JSONResponse(out, status_code=code)


# --- home y listados --------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render("index.html", request, items=db.latest(42), stats=db.stats(), page_title=None)


@app.get("/peliculas", response_class=HTMLResponse)
def movies(request: Request):
    page = _page(request, "page")
    items, total = db.catalog("movie", page)
    return render("list.html", request, items=items, page=page, total=total,
                  per_page=42, kind="movie", section="Peliculas",
                  page_title=f"Películas · página {page}")


@app.get("/series", response_class=HTMLResponse)
def series_list(request: Request):
    page = _page(request, "page")
    items, total = db.catalog("series", page)
    return render("list.html", request, items=items, page=page, total=total,
                  per_page=42, kind="series", section="Series",
                  page_title=f"Series · página {page}")


@app.get("/buscar", response_class=HTMLResponse)
def buscar(request: Request):
    qs = (request.query_params.get("q") or "").strip()
    items = db.search(qs) if qs else []
    return render("list.html", request, items=items, page=1, total=len(items),
                  per_page=42, kind=None, section=f"Resultados para «{qs}»",
                  page_title=f"Buscar: {qs}")


# --- detalle ----------------------------------------------------------------

@app.get("/pelicula/{slug}", response_class=HTMLResponse)
def movie(request: Request, slug: str):
    t = db.find_title("movie", slug)
    if not t:
        raise HTTPException(404, "No encontrada")
    return render("movie.html", request, t=t, streams=db.streams_for(t["id"]),
                  rel=db.related(t), page_title=t["title"])


@app.get("/serie/{slug}", response_class=HTMLResponse)
def serie(request: Request, slug: str):
    t = db.find_title("series", slug)
    if not t:
        raise HTTPException(404, "No encontrada")
    return render("serie.html", request, t=t, seasons=db.seasons(t["id"]),
                  page_title=t["title"])


@app.get("/serie/{slug}/temporada/{season}", response_class=HTMLResponse)
def season(request: Request, slug: str, season: int):
    t = db.find_title("series", slug)
    if not t:
        raise HTTPException(404, "No encontrada")
    return render("season.html", request, t=t, season=season,
                  eps=db.episodes(t["id"], season), seasons=db.seasons(t["id"]),
                  page_title=f"{t['title']} · Temporada {season}")


@app.get("/serie/{slug}/episodio/{sxe}", response_class=HTMLResponse)
def episode(request: Request, slug: str, sxe: str):
    t = db.find_title("series", slug)
    if not t:
        raise HTTPException(404, "No encontrada")
    try:
        s, e = (int(x) for x in sxe.lower().split("x"))
    except Exception:
        raise HTTPException(400, "Formato de episodio inválido")
    ep = db.find_episode(t["id"], s, e)
    if not ep:
        raise HTTPException(404, "Episodio no encontrado")
    return render("episode.html", request, t=t, ep=ep, season=s,
                  streams=db.streams_for(t["id"], ep["id"]),
                  page_title=f"{t['title']} {s}x{e}")


# --- resolución de stream ---------------------------------------------------

@app.get("/api/stream/{stream_id}")
def api_stream(stream_id: int):
    """Devuelve el master.m3u8 del servidor pedido, resolviéndolo si caducó."""
    row = db.q1(
        """SELECT s.id, s.m3u8_url, s.expires_at, s.status, s.embed_url, s.title_id
             FROM streams s WHERE s.id = %s""",
        (stream_id,),
    )
    if not row:
        raise HTTPException(404, "stream no encontrado")

    now = int(datetime.now(timezone.utc).timestamp())
    fresh = (
        row["m3u8_url"]
        and row["status"] == "ok"
        and row["expires_at"]
        and int(row["expires_at"].timestamp()) > now + 300
    )
    if fresh:
        return {"url": row["m3u8_url"], "cached": True}

    # Resolver on-demand. Si la fila no tiene embed_url (backfill incompleto),
    # se avisa en vez de intentar adivinar.
    if not row["embed_url"]:
        raise HTTPException(409, "este servidor aún no está resuelto")

    try:
        got = resolve(http(), row["embed_url"])
    except ResolveError as exc:
        db.execute(
            "UPDATE streams SET status='error', fail_count=fail_count+1 WHERE id=%s",
            (stream_id,),
        )
        raise HTTPException(502, f"no se pudo resolver ({exc.step})")

    ok, code, _ = check_master(http(), got["m3u8_url"])
    if not ok:
        db.execute(
            "UPDATE streams SET status='dead', fail_count=fail_count+1 WHERE id=%s",
            (stream_id,),
        )
        raise HTTPException(502, f"el master no responde (HTTP {code})")

    db.execute(
        """UPDATE streams
              SET m3u8_url=%s, resolved_at=now(),
                  expires_at=to_timestamp(%s), status='ok', fail_count=0
            WHERE id=%s""",
        (got["m3u8_url"], got["expires_at"], stream_id),
    )
    return {"url": got["m3u8_url"], "cached": False}


# --- sitemaps y robots ------------------------------------------------------

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap_index():
    n = db.q1("SELECT count(*) AS n FROM titles WHERE active")["n"]
    pages = max(1, (n + 49999) // 50000)
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for i in range(1, pages + 1):
        body.append(f"<sitemap><loc>{SITE_URL}/sitemap-{i}.xml</loc></sitemap>")
    body.append("</sitemapindex>")
    return Response("\n".join(body), media_type="application/xml")


@app.get("/sitemap-{n}.xml")
def sitemap_page(n: int):
    off = (n - 1) * 50000
    rows = db.q(
        """SELECT kind, slug, last_seen FROM titles WHERE active
            ORDER BY id LIMIT 50000 OFFSET %s""",
        (off,),
    )
    if not rows:
        raise HTTPException(404)
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for r in rows:
        path = "pelicula" if r["kind"] == "movie" else "serie"
        lastmod = r["last_seen"].strftime("%Y-%m-%d") if r["last_seen"] else ""
        body.append(
            f"<url><loc>{SITE_URL}/{path}/{r['slug']}</loc>"
            f"<lastmod>{lastmod}</lastmod></url>"
        )
    body.append("</urlset>")
    return Response("\n".join(body), media_type="application/xml")
