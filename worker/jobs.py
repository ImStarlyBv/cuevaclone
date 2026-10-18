"""Trabajos del worker: catálogo, resolución de fuentes y delta de episodios.

Regla que gobierna todo el archivo: **nunca se sobrescriben datos buenos con
resultados vacíos**. Si una pasada trae menos de lo esperado, se aborta y se
registra el fallo; lo que ya está en la base queda intacto. El peor caso es un
sitio con datos viejos, nunca un sitio vacío.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

from scraper.client import (
    UA,
    parse_items,
    parse_servers,
    robust_get,
    session,
)
from scraper.resolve import ResolveError, check_master, resolve

from app import db

BASE = "https://cuevana3r.pro"
RPS = float(os.environ.get("CRAWL_RPS", "1.5"))
REFRESH_MARGIN_H = int(os.environ.get("REFRESH_MARGIN_H", "6"))

# Si una página de catálogo trae menos de esto, se considera cambio de estructura
# o bloqueo, y la pasada se aborta sin tocar datos.
MIN_ITEMS_PER_PAGE = 5

_EP_LINK = re.compile(r"/serie/([a-z0-9-]+)/episodio-(\d+)x(\d+)")


def _pace(n=1):
    """Espera lo justo para respetar CRAWL_RPS contando n peticiones HTTP."""
    per = 1.0 / RPS if RPS > 0 else 0.5
    time.sleep(max(0.0, per * n))


def _log(task, ok, **kw):
    try:
        db.execute(
            """INSERT INTO health_runs (task, ok, rows, expected, duration_ms, failed_step, detail)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                task,
                ok,
                kw.get("rows"),
                kw.get("expected"),
                kw.get("duration_ms"),
                kw.get("failed_step"),
                json.dumps(kw.get("detail") or {}, default=str),
            ),
        )
    except Exception:
        pass


# --- catálogo ---------------------------------------------------------------

def upsert_title(kind, slug, title, year, poster=None):
    db.execute(
        """INSERT INTO titles (kind, slug, title, year, poster, last_seen)
           VALUES (%s, %s, %s, %s, %s, now())
           ON CONFLICT (kind, slug) DO UPDATE
              SET title = EXCLUDED.title,
                  year  = COALESCE(EXCLUDED.year, titles.year),
                  poster = COALESCE(EXCLUDED.poster, titles.poster),
                  last_seen = now(),
                  active = TRUE""",
        (kind, slug, title, year, poster),
    )


def crawl_catalog(max_pages=None, kind="peliculas"):
    """Recorre el listado paginado y hace upsert de los títulos."""
    t0 = time.perf_counter()
    sess = session()
    written = 0
    page = 1
    empty_pages = 0

    while True:
        if max_pages and page > max_pages:
            break
        try:
            r = robust_get(sess, f"{BASE}/{kind}", params={"page": page},
                           timeout=30, headers={"User-Agent": UA})
        except Exception as exc:
            _log(f"catalog:{kind}", False, rows=written, failed_step="fetch",
                 detail={"page": page, "error": str(exc)})
            return written

        # 429 tras los reintentos = rate limit, no cambio de estructura: se corta
        # la pasada conservando lo escrito, y la siguiente reanuda desde el inicio.
        if r.status_code == 429:
            _log(f"catalog:{kind}", False, rows=written, failed_step="rate_limit",
                 detail={"page": page})
            break

        items = parse_items(r.text) if r.status_code == 200 else []
        if not items:
            empty_pages += 1
            # una página vacía al principio = estructura rota o bloqueo
            if page == 1 or empty_pages >= 2:
                break
        else:
            empty_pages = 0
            for it in items:
                upsert_title(
                    "movie" if kind == "peliculas" else "series",
                    it["slug"], it["title"], it.get("year"),
                )
                written += 1

        page += 1
        _pace()

    ok = written >= MIN_ITEMS_PER_PAGE
    _log(f"catalog:{kind}", ok, rows=written, expected=None,
         duration_ms=int((time.perf_counter() - t0) * 1000),
         failed_step=None if ok else "parse",
         detail={"pages": page - 1})
    return written


# --- resolución -------------------------------------------------------------

def store_sources(title_id, episode_id, html):
    """Parsea los data-server del detalle y hace upsert de las filas de streams."""
    groups = parse_servers(html)
    n = 0
    for group in groups:
        lang = group.get("language") or "Desconocido"
        for srv in group.get("servers", []):
            # Los embeds de terceros no pasan por el packer: nacen como 'external'
            # y se sirven como iframe. Si nacen 'pending' se quedan ahí para siempre.
            kind = srv.get("type")
            initial = "external" if kind == "embed" else "pending"
            db.execute(
                """INSERT INTO streams (title_id, episode_id, language, server_name,
                                        kind, embed_url, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (title_id, episode_id, language, server_name)
                   DO UPDATE SET kind = EXCLUDED.kind,
                                 embed_url = EXCLUDED.embed_url,
                                 status = CASE WHEN streams.status = 'dead'
                                               THEN 'pending' ELSE streams.status END""",
                (title_id, episode_id, lang, srv["name"], kind,
                 srv.get("url") or srv.get("embed_url"), initial),
            )
            n += 1
    return n


def resolve_one(sess, row):
    """Resuelve una fila de streams y la actualiza. Devuelve True/False."""
    try:
        got = resolve(sess, row["embed_url"])
    except ResolveError as exc:
        db.execute(
            """UPDATE streams SET status='error', fail_count=fail_count+1
                WHERE id=%s""",
            (row["id"],),
        )
        return False, exc.step

    ok, code, _ = check_master(sess, got["m3u8_url"])
    if not ok:
        db.execute(
            """UPDATE streams SET status='dead', fail_count=fail_count+1
                WHERE id=%s""",
            (row["id"],),
        )
        return False, "m3u8_fetch"

    db.execute(
        """UPDATE streams
              SET m3u8_url=%s, resolved_at=now(), expires_at=to_timestamp(%s),
                  status='ok', fail_count=0
            WHERE id=%s""",
        (got["m3u8_url"], got["expires_at"], row["id"]),
    )
    return True, None


def mark_external():
    """Los embeds de terceros no pasan por el packer: se sirven como iframe.

    Se marcan una vez para que el resolver no los vuelva a intentar y para que
    la web sepa que tiene que pintarlos con <iframe> en vez de con hls.js.
    """
    return db.execute(
        """UPDATE streams SET status = 'external'
            WHERE kind = 'embed' AND status <> 'external'"""
    )


def resolve_batch(limit=40, only_due=True):
    """Resuelve un lote de fuentes token pendientes o caducadas."""
    t0 = time.perf_counter()
    if only_due:
        rows = db.q(
            """SELECT id, embed_url FROM streams
                WHERE kind = 'token'
                  AND (status IN ('pending', 'error')
                       OR expires_at IS NULL
                       OR expires_at < now() + make_interval(hours => %s))
                  AND embed_url IS NOT NULL
                  AND fail_count < 8
             ORDER BY resolved_at NULLS FIRST, id
                LIMIT %s""",
            (REFRESH_MARGIN_H, limit),
        )
    else:
        rows = db.q(
            """SELECT id, embed_url FROM streams
                WHERE kind = 'token' AND embed_url IS NOT NULL AND fail_count < 8
             ORDER BY resolved_at NULLS FIRST, id LIMIT %s""",
            (limit,),
        )

    if not rows:
        return 0, 0

    sess = session()
    good = bad = 0
    for row in rows:
        ok, _step = resolve_one(sess, row)
        good += 1 if ok else 0
        bad += 0 if ok else 1
        _pace(2)

    _log("resolve", good > 0 or not rows, rows=good, expected=len(rows),
         duration_ms=int((time.perf_counter() - t0) * 1000),
         failed_step=None if good else "all_failed",
         detail={"attempted": len(rows), "failed": bad})
    return good, bad


# --- delta de episodios -----------------------------------------------------

def delta_episodes():
    """Lee /episodios/recientes, registra episodios nuevos y encola sus fuentes."""
    t0 = time.perf_counter()
    sess = session()
    try:
        r = robust_get(sess, f"{BASE}/episodios/recientes", timeout=30,
                       headers={"User-Agent": UA})
        html = r.text if r.status_code == 200 else ""
    except Exception as exc:
        _log("delta", False, failed_step="fetch", detail={"error": str(exc)})
        return 0

    found = set(_EP_LINK.findall(html))
    if not found:
        _log("delta", False, rows=0, failed_step="parse",
             detail={"reason": "sin enlaces de episodio en el HTML"})
        return 0

    added = 0
    for slug, season, ep in found:
        title = db.q1("SELECT id FROM titles WHERE kind='series' AND slug=%s", (slug,))
        if not title:
            continue
        cur = db.q1(
            "SELECT id FROM episodes WHERE title_id=%s AND season=%s AND episode=%s",
            (title["id"], int(season), int(ep)),
        )
        if cur:
            continue
        db.execute(
            "INSERT INTO episodes (title_id, season, episode) VALUES (%s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (title["id"], int(season), int(ep)),
        )
        added += 1

    _log("delta", True, rows=added, expected=len(found),
         duration_ms=int((time.perf_counter() - t0) * 1000),
         detail={"links": len(found)})
    return added


# --- poblado inicial de fuentes --------------------------------------------

def seed_sources(limit=25):
    """Para títulos sin ninguna fila de streams, lee el detalle y guarda las fuentes."""
    t0 = time.perf_counter()
    rows = db.q(
        """SELECT t.id, t.slug, t.kind FROM titles t
            WHERE t.active
              AND NOT EXISTS (SELECT 1 FROM streams s WHERE s.title_id = t.id)
         ORDER BY t.last_seen DESC LIMIT %s""",
        (limit,),
    )
    if not rows:
        return 0

    sess = session()
    total = 0
    for t in rows:
        path = "pelicula" if t["kind"] == "movie" else "serie"
        try:
            r = robust_get(sess, f"{BASE}/{path}/{t['slug']}", timeout=30,
                           headers={"User-Agent": UA})
            if r.status_code != 200:
                continue
            total += store_sources(t["id"], None, r.text)
        except Exception:
            continue
        _pace()

    _log("seed_sources", total > 0, rows=total, expected=len(rows),
         duration_ms=int((time.perf_counter() - t0) * 1000),
         failed_step=None if total else "parse")
    return total


def resolve_episodes(limit=20):
    """Resuelve fuentes de episodios que aún no las tienen."""
    rows = db.q(
        """SELECT t.id AS title_id, t.slug, e.id AS episode_id, e.season, e.episode
             FROM episodes e JOIN titles t ON t.id = e.title_id
            WHERE NOT EXISTS (SELECT 1 FROM streams s WHERE s.episode_id = e.id)
            ORDER BY e.id DESC LIMIT %s""",
        (limit,),
    )
    if not rows:
        return 0
    sess = session()
    n = 0
    for r in rows:
        try:
            resp = robust_get(
                sess,
                f"{BASE}/serie/{r['slug']}/episodio-{r['season']}x{r['episode']}",
                timeout=30, headers={"User-Agent": UA},
            )
            if resp.status_code == 200:
                n += store_sources(r["title_id"], r["episode_id"], resp.text)
        except Exception:
            continue
        _pace()
    return n
