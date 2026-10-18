"""Capa de datos: pool de Postgres y las consultas que usa la web."""

import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:5432/streaming"
)

POOL = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=8,
    timeout=10,
    kwargs={"row_factory": dict_row},
    open=False,
)


def open_pool():
    if POOL.closed:
        POOL.open(wait=True, timeout=20)


def init_schema():
    """Aplica schema.sql. Idempotente."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "schema.sql"), encoding="utf-8") as fh:
        sql = fh.read()
    with POOL.connection() as conn:
        conn.execute(sql)
        conn.commit()


def q(sql, params=None):
    with POOL.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def q1(sql, params=None):
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    with POOL.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


# --- consultas de la web ----------------------------------------------------

def latest(limit=42, kind=None):
    if kind:
        return q(
            """SELECT id, kind, slug, title, year, poster, rating
                 FROM titles WHERE active AND kind = %s
                ORDER BY last_seen DESC LIMIT %s""",
            (kind, limit),
        )
    return q(
        """SELECT id, kind, slug, title, year, poster, rating
             FROM titles WHERE active
            ORDER BY last_seen DESC LIMIT %s""",
        (limit,),
    )


def catalog(kind, page=1, per_page=42):
    off = max(0, (page - 1) * per_page)
    rows = q(
        """SELECT id, kind, slug, title, year, poster, rating
             FROM titles WHERE active AND kind = %s
            ORDER BY last_seen DESC LIMIT %s OFFSET %s""",
        (kind, per_page, off),
    )
    total = q1(
        "SELECT count(*) AS n FROM titles WHERE active AND kind = %s", (kind,)
    )["n"]
    return rows, total


def find_title(kind, slug):
    return q1(
        """SELECT id, kind, slug, title, year, tmdb_id, poster, synopsis, rating
             FROM titles WHERE kind = %s AND slug = %s""",
        (kind, slug),
    )


def streams_for(title_id, episode_id=None):
    if episode_id is None:
        rows = q(
            """SELECT id, language, server_name, m3u8_url, expires_at, status, fail_count
                 FROM streams
                WHERE title_id = %s AND episode_id IS NULL
             ORDER BY language, server_name""",
            (title_id,),
        )
    else:
        rows = q(
            """SELECT id, language, server_name, m3u8_url, expires_at, status, fail_count
                 FROM streams
                WHERE title_id = %s AND episode_id = %s
             ORDER BY language, server_name""",
            (title_id, episode_id),
        )
    return rows


def seasons(title_id):
    return q(
        """SELECT season, count(*) AS n, min(episode) AS first_ep, max(episode) AS last_ep
             FROM episodes WHERE title_id = %s
            GROUP BY season ORDER BY season""",
        (title_id,),
    )


def episodes(title_id, season):
    return q(
        """SELECT id, season, episode FROM episodes
            WHERE title_id = %s AND season = %s ORDER BY episode""",
        (title_id, season),
    )


def find_episode(title_id, season, episode):
    return q1(
        """SELECT id, season, episode FROM episodes
            WHERE title_id = %s AND season = %s AND episode = %s""",
        (title_id, season, episode),
    )


def search(query, limit=42):
    like = f"%{query}%"
    return q(
        """SELECT id, kind, slug, title, year, poster, rating
             FROM titles
            WHERE active AND title ILIKE %s
         ORDER BY similarity(title, %s) DESC, year DESC NULLS LAST
            LIMIT %s""",
        (like, query, limit),
    )


def related(title, limit=12):
    row = q1("SELECT id FROM titles WHERE kind = %s AND slug = %s",
             (title["kind"], title["slug"]))
    if not row:
        return []
    return q(
        """SELECT id, kind, slug, title, year, poster, rating
             FROM titles
            WHERE active AND id <> %s
              AND kind = %s
              AND abs(coalesce(year, 0) - coalesce(%s, 0)) <= 3
         ORDER BY similarity(title, %s) DESC
            LIMIT %s""",
        (row["id"], title["kind"], title["year"], title["title"], limit),
    )


def stats():
    return q1(
        """SELECT
             (SELECT count(*) FROM titles WHERE active)                        AS titles,
             (SELECT count(*) FROM titles WHERE active AND kind = 'movie')     AS movies,
             (SELECT count(*) FROM titles WHERE active AND kind = 'series')    AS series,
             (SELECT count(*) FROM streams WHERE status = 'ok')                AS streams_ok,
             (SELECT count(*) FROM streams WHERE status <> 'ok')               AS streams_bad"""
    )
