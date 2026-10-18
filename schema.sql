-- Esquema de cuevaclone. Idempotente: se aplica en cada arranque.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS titles (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('movie', 'series')),
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    year        SMALLINT,
    tmdb_id     INTEGER,
    poster      TEXT,
    synopsis    TEXT,
    rating      NUMERIC(3,1),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, slug)
);

CREATE INDEX IF NOT EXISTS titles_trgm ON titles USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS titles_kind_seen ON titles (kind, last_seen DESC);

CREATE TABLE IF NOT EXISTS episodes (
    id          BIGSERIAL PRIMARY KEY,
    title_id    BIGINT NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    season      SMALLINT NOT NULL,
    episode     SMALLINT NOT NULL,
    UNIQUE (title_id, season, episode)
);

CREATE TABLE IF NOT EXISTS streams (
    id           BIGSERIAL PRIMARY KEY,
    title_id     BIGINT NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    episode_id   BIGINT REFERENCES episodes(id) ON DELETE CASCADE,
    language     TEXT NOT NULL,
    server_name  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    file_id      TEXT,
    embed_url    TEXT,
    m3u8_url     TEXT,
    resolved_at  TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'pending',
    fail_count   SMALLINT NOT NULL DEFAULT 0,
    UNIQUE (title_id, episode_id, language, server_name)
);

CREATE INDEX IF NOT EXISTS streams_due ON streams (status, expires_at);
CREATE INDEX IF NOT EXISTS streams_title ON streams (title_id, episode_id);

-- Registro de pasadas del worker y del canario. Es lo que lee el cron de
-- supervisión: el reporte no adivina, lee esta tabla.
CREATE TABLE IF NOT EXISTS health_runs (
    id          BIGSERIAL PRIMARY KEY,
    ran_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    task        TEXT NOT NULL,
    ok          BOOLEAN NOT NULL,
    rows        INTEGER,
    expected    INTEGER,
    duration_ms INTEGER,
    failed_step TEXT,
    detail      JSONB
);

CREATE INDEX IF NOT EXISTS health_runs_recent ON health_runs (ran_at DESC);
