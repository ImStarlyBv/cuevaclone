# Plan de montaje — sitio de streaming (modelo cuevana)

Basado en el recon verificado de `cuevana3r.pro` (`cuevana3r-api.md`) y en el cliente ya
funcional (`cuevana3r_client.py`).

## 0. Lo que ya está resuelto

| Pieza | Estado | Coste |
|---|---|---|
| Catálogo completo | endpoints mapeados, 938 páginas / ~16.9k títulos | 1 GET por página |
| Búsqueda | `/explorar?s=` | 1 GET |
| Delta diario | `/episodios/recientes` | 1 GET cada 15 min |
| Fuentes por título | `data-server` + XOR decodificado | 1 GET por título |
| m3u8 final | unpack del packer, verificado hasta el segmento | 1 GET por título |
| Ancho de banda de video | **cero** — el CDN no valida Referer, se hotlinkea | $0 |

El pipeline de datos no es el problema. El problema es la web, el SEO y la operación.

---

## 1. Principio de construcción

**Rebanada vertical primero.** Un título, de punta a punta, en producción, antes de escalar a
17.000. Si el flujo `slug -> token -> file_id -> m3u8 -> player en el navegador` funciona para uno,
funciona para todos. Si lo montas al revés (backfill de 17k y luego la web), descubres los
problemas de arquitectura con la base de datos ya llena.

Orden real de trabajo:

```
1 título en producción  ->  100 títulos  ->  catálogo completo  ->  SEO a escala
```

---

## 2. Stack

Un solo stack de **Docker Compose desplegado como Service en Coolify** (VPS 147.93.188.255).
No apps separadas: el servicio comparte red, volúmenes y ciclo de vida, y Coolify lo trata como una
unidad desplegable.

```
Cloudflare (DNS + WAF + cache)
        |
        v
   [coolify-proxy / Traefik]
        |
        v
  servicio "streaming"  (docker compose)
  ------------------------------------------------
   web      FastAPI + Jinja2 SSR   :8000   (expuesta)
   worker   asyncio: crawler + resolver + scheduler
   db       postgres:16-alpine     volumen pgdata
   redis    redis:7-alpine         maxmemory 128mb
  ------------------------------------------------
```

```yaml
# docker-compose.yaml (la que se pega en Coolify -> Service)
services:
  web:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    environment:
      DATABASE_URL: postgresql://app:${DB_PASS}@db:5432/streaming
      REDIS_URL: redis://redis:6379/0
    depends_on: [db, redis]
    restart: unless-stopped

  worker:
    build: .
    command: python -m worker.main
    environment:
      DATABASE_URL: postgresql://app:${DB_PASS}@db:5432/streaming
      REDIS_URL: redis://redis:6379/0
      CRAWL_RPS: "2"
    depends_on: [db, redis]
    restart: unless-stopped

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${DB_PASS}
      POSTGRES_DB: streaming
    volumes: [pgdata:/var/lib/postgresql/data]
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 128mb --maxmemory-policy allkeys-lru
    restart: unless-stopped

volumes:
  pgdata:
```

Elección y por qué:

- **FastAPI + Jinja2, no Next.js.** SSR puro sin build step, sin hidratación, TTFB mínimo, y es el
  mismo lenguaje que el crawler (todo el código es Python). Para 17k páginas generadas
  programáticamente un framework SPA-react solo añade piezas que se rompen. Next.js sería la
  alternativa si quisieras ISR, pero aquí el cache de Redis + Cloudflare hace lo mismo con menos
  superficie.
- **Postgres** y no SQLite: escrituras concurrentes del worker mientras la web lee, y búsqueda de
  texto completo con `pg_trgm` para el buscador interno.
- **Redis** para cache de página (TTL corto) y como cola del resolver.
- **hls.js vanilla** para el player. Sin bundler.
- **worker con scheduler interno** (APScheduler), no cron del host: los bucles operativos (delta de
  episodios cada 15 min, refresco de streams) viven dentro del contenedor y sobreviven a reinicios
  de Coolify. El cron de Hermes es para supervisión y reporte, no para la operación.

### Qué hace exactamente el worker

Tres tareas, todas HTTP + CPU despreciable. Ninguna toca video.

| Tarea | Frecuencia | Peticiones |
|---|---|---|
| `crawl_catalog` — recorre `/peliculas?page=1..938` y `/series?page=1..N`, parsea slugs y guarda | una vez, y luego cada 24 h | ~1.000 por pasada |
| `resolve` — por título: GET detalle → GET embed → unpack → guarda `m3u8_url` | continuo, gobernado por `expires_at` | 2 por título |
| `delta` — GET `/episodios/recientes`, inserta lo nuevo y lo encola para resolver | cada 15 min | 96/día |

**Backfill inicial, aritmética honesta:**

```
17.000 títulos × 2 requests  = 34.000 requests
a 2 req/s                    = 4,7 h
a 3 req/s                    = 3,1 h
```

Corrijo lo que dije antes: son ~3–5 h, no 2 h. La pasada del catálogo sola son ~8 min; lo que pesa
es resolver los 17k, porque cada uno son dos GET.

**Régimen permanente:**

```
renovación: 17.000 streams, validez 36 h, se refrescan al quedar 6 h
            -> cada stream se renueva ~cada 30 h
            -> 17.000/30 h × 2 requests = ~1.130 req/h  =  0,31 req/s
delta:      96 req/día
```

O sea: **~27.000 requests al día en estado estacionario, 0,3 req/s**. Es menos tráfico del que
genera una sola visita humana navegando el sitio.

**Consumo de memoria real del worker:**

- Proceso Python + httpx en asyncio: 60–80 MB de base.
- 20 conexiones concurrentes con HTML de 15–80 KB en vuelo: ~2 MB transitorios, se liberan por título.
- No acumula estado: parsea, escribe una fila, descarta.
- **Peak RSS esperado: 150–200 MB.**

CPU: **34 ms por título**, medido sobre el packer real, con `unpack.py` (ya escrito y verificado
byte a byte contra la salida de node). El backfill completo son ~10 min de CPU repartidos entre
3–5 h de red. En régimen permanente: 17.000 / 30 h × 34 ms ≈ 5 s de CPU por hora.

Un `node` por título habría sido 17.000 arranques de V8 (~80 ms cada uno solo de overhead, ~22 min)
más el coste de memoria por fork. **Decisión tomada: unpacker portado, sin shell-out.**

Los dos bugs que costó portarlo, y que quedan resueltos en el módulo:

- `\b` es Unicode-aware en Python y no en JS — con acentos el unpack sale corrupto. `flags=re.ASCII`.
- El payload es un literal JS de comillas simples: hay que desescapar `\'` antes de procesar, o
  la salida conserva backslashes que node no produce.

### Por qué entonces me preocupa la RAM

No por el worker. Por dos cosas distintas:

1. **El total del stack en una caja con 3 GB libres.** Postgres 256 MB de shared buffers + Redis
   128 MB + web 200 MB + worker 200 MB ≈ 800 MB. Cabe, pero el VPS ya tiene 44 contenedores
   compitiendo y el margen se estrecha si cualquiera de ellos decide crecer.
2. **El headless browser, que descartamos.** Ahí sí el argumento era correcto: un Chromium por
   contexto son 300–800 MB. Eso no cabe en 3 GB con todo lo demás encima. Por eso los embeds de
   terceros quedan fuera y nos quedamos con el host propio, que solo necesita HTTP.

El worker es la pieza más barata del stack. Lo que no cabe es Chromium.

### Capacidad real del VPS (medido, no estimado)

```
6 vCPU | 11 GB RAM de los que 8 ya están usados -> 3 GB disponibles
44 contenedores corriendo | disco 132/193 GB (69%)
```

El stack completo consume ~700 MB–1 GB en régimen (Postgres 256 MB de shared_buffers, Redis 128 MB,
web 200 MB, worker 150 MB). Cabe, pero con poco margen: hay que poner límites explícitos en el
compose (`mem_limit`) y vigilar. Si el catálogo crece o añades headless browser para los embeds de
terceros, eso ya no cabe aquí y toca segundo VPS.

---

## 3. Esquema de datos

```sql
-- catálogo
CREATE TABLE titles (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('movie','series')),
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    year        SMALLINT,
    tmdb_id     INTEGER,
    poster      TEXT,
    synopsis    TEXT,
    rating      NUMERIC(3,1),
    source_site TEXT DEFAULT 'cuevana',
    first_seen  TIMESTAMPTZ DEFAULT now(),
    last_seen   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (kind, slug)
);

CREATE TABLE episodes (
    id          BIGSERIAL PRIMARY KEY,
    title_id    BIGINT REFERENCES titles(id) ON DELETE CASCADE,
    season      SMALLINT NOT NULL,
    episode     SMALLINT NOT NULL,
    air_date    DATE,
    UNIQUE (title_id, season, episode)
);

-- fuentes resueltas (una fila por servidor e idioma)
CREATE TABLE streams (
    id           BIGSERIAL PRIMARY KEY,
    title_id     BIGINT REFERENCES titles(id) ON DELETE CASCADE,
    episode_id   BIGINT REFERENCES episodes(id) ON DELETE CASCADE,
    language     TEXT NOT NULL,          -- Latino | Castellano | Subtitulado
    server_name  TEXT NOT NULL,          -- Hyper | Nebula | Goldmember | ...
    kind         TEXT NOT NULL,          -- 'token' | 'embed'
    file_id      TEXT,
    embed_url    TEXT,
    m3u8_url     TEXT,
    resolved_at  TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ,            -- resolved_at + 36h - margen
    status       TEXT DEFAULT 'ok',      -- ok | dead | expired | error
    fail_count   SMALLINT DEFAULT 0,
    UNIQUE (title_id, episode_id, language, server_name)
);

CREATE INDEX ON streams (status, expires_at);
CREATE INDEX ON titles USING gin (title gin_trgm_ops);
```

`expires_at` es la columna que gobierna todo: el scheduler solo refresca lo que está por caducar.

---

## 4. Fases

### Fase 1 — Pipeline de datos (días 1–3)

- Schema + migraciones.
- `catalog_crawler`: recorre `?page=1..938` de películas y series a 2 req/s. ~15 min por pasada.
- `resolver`: por título, 3 pasos HTTP (detalle → embed → unpack). Escribe `streams`.
- `scheduler`: `/episodios/recientes` cada 15 min; refresco de `streams` cuando `expires_at < now()+6h`.
- Backfill del catálogo corriendo en background (~2 h) mientras se construye la web.

Entregable: base de datos con 17k títulos y streams resueltos. Comprobable con un `SELECT`.

### Fase 2 — Web + player (días 4–7)

Rutas (todas SSR, todas con `<title>`/meta/JSON-LD):

```
/                                  home, últimos añadidos
/pelicula/{slug}                   detalle + player + servidores
/serie/{slug}                      temporadas
/serie/{slug}/temporada/{n}        episodios
/serie/{slug}/episodio/{s}x{e}     detalle + player
/peliculas?page=N                  paginado (mismo esquema que el origen)
/series?page=N
/genero/{g}  /anio/{y}  /idioma/{l}   hubs para enlazado interno
/buscar?q=
```

Player:

- `hls.js` + controles propios.
- Selector de servidor e idioma, alimentado desde `streams`.
- Si `m3u8_url` caducó → llamada a `/api/stream/{id}` que dispara el resolver on-demand y
  devuelve el nuevo master. El usuario ve un spinner, no un 404.
- Fallback automático: si el primer servidor falla en el manifest, salta al siguiente y marca
  `fail_count++` en el servidor muerto.

Entregable: un título reproducible en navegador real, desde tu dominio.

### Fase 3 — SEO (días 8–12)

- `sitemap.xml` segmentado en 50k URLs por archivo + índice. Regenerado por el worker.
- Páginas programáticas por título: la página de detalle ya es la doorway si el `<title>`, el H1 y
  la sinopsis están bien generados. No necesitas un sistema aparte de doorway pages — el propio
  detalle lo es.
- Enlazado interno: cada detalle enlaza a relacionados por género, actor, saga, año. Sin esto el
  crawler no llega a las 17k páginas.
- JSON-LD `Movie`/`TVSeries` + `BreadcrumbList` + `AggregateRating`.
- Hubs de género/año/idioma para capturar consultas amplias.
- `robots.txt` permisivo, sin `noindex` en el detalle.

Entregable: indexación empezando a subir, primeras impresiones en Search Console.

### Fase 4 — Monetización + operación (días 13–16)

- Ads: capa de VAST o script propio sobre el player (popunder + banner + pre-roll). Empezar con una
  red que acepte este vertical; tener la segunda lista antes de que baneen la primera.
- Monitorización: cron que resuelve 3 títulos canario cada hora y avisa si cambian el packer, la
  clave XOR o el host. Esto es lo que evita que te enteres por los usuarios.
- Churn de dominios: registro de dominios, redirect 301 automático, sitemap por dominio, aviso de
  caída.
- Panel mínimo: tráfico, títulos con más vistas, streams muertos, tasa de fallo del player.

---

## 5. Despliegue en Coolify

Instancia: `http://147.93.188.255:8000` (Coolify 4.4.6, 1 servidor, 37 apps, 8 services).
Cliente local: `python C:\Users\l_urb\coolify\coolify.py`.

### 5.1 Alta del servicio

1. Repo en GitHub (`ImStarlyBv/…`) con `Dockerfile`, `docker-compose.yaml` y `app/`, `worker/`.
2. Coolify → **+ New** → **Service** → *Docker Compose* → conectar el repo, rama `main`, base
   directory `/`.
3. Añadir los env de producción: `DB_PASS`, `DATABASE_URL`, `REDIS_URL`, `CRAWL_RPS`, `SITE_NAME`,
   `ADS_*`. **Ponerlos antes del primer deploy** — un env edit no reinicia nada, el valor se aplica
   al crear el contenedor siguiente, así que configurarlo después cuesta un rebuild extra.
4. Exponer solo `web:8000`. Traefik etiqueta el sub-servicio que tenga `SERVICE_FQDN_WEB_8000`.
   `db` y `redis` sin puerto publicado, solo red interna.
5. Deploy. Poll de `GET /deployments` emparejando `deployment_uuid`; la lista solo contiene las
   ejecuciones **en curso** (una terminada desaparece, parece que se evaporó).
6. Verificar contra la URL pública, no contra el panel: `curl -s -o /dev/null -w "%{http_code}"`.
   Cloudflare devuelve **403** si el User-Agent es `Python-urllib/3.x` y 200 con UA de navegador —
   falsear el UA en los scripts de verificación o se reporta un deploy sano como roto.

### 5.2 Notas de operación del stack

- `PATCH /services/{uuid}` exige `docker_compose_raw` **en base64** (YAML plano → 422).
- Al borrar una clave del compose, borrar también su ítem de secuencia: una `ports:` huérfana se
  pliega en el escalar anterior y el contenedor no se crea nunca, mientras la API sigue devolviendo
  200. Releer el raw después de cada patch y afirmar las líneas exactas.
- Los nombres de contenedor son `<servicio>-<uuid>-<buildid>` y el sufijo cambia en cada redeploy:
  resolverlos con `docker ps --format '{{.Names}}' | grep -m1 '^web-<uuid>'`, nunca cachearlos.
- `POST /services/{uuid}/scheduled-tasks` acepta comandos de **máx. ~255 chars**; para algo más
  largo, script en el repo y comando corto que lo invoque.

---

## 6. Mantenimiento diario (lo que corro yo)

Reparto de responsabilidades, y es deliberado:

| Qué | Dónde | Por qué |
|---|---|---|
| Delta de episodios cada 15 min | scheduler **dentro del worker** | operación, sobrevive a mi ausencia |
| Refresco de streams por `expires_at` | scheduler **dentro del worker** | idem |
| Supervisión + reporte diario | **cron de Hermes** | esto sí es mío |

Un job de Hermes no debe ser la pieza que mantiene vivo el sitio: si el gateway está caído, no
dispara. Los bucles operativos van en el contenedor; el cron externo solo mira y avisa.

### 6.1 Job diario de supervisión

`hermes cron create "every day at 7am"` → reporte a Telegram (desde CLI el `deliver` local no
notifica). Qué comprueba:

1. `GET /api/health` de Coolify + estado del servicio `web` (`running:healthy`).
2. `GET /healthz` de la web: responde 200 y `db: ok`, `redis: ok`.
3. Conteos en Postgres: `titles`, `streams` con `status='ok'`, `streams` con `expires_at < now()`.
   Alerta si el % de streams sanos baja del 90%.
4. **Canario de resolución**: resolver 3 títulos fijos de punta a punta
   (token → embed → unpack → m3u8 → `curl` al master). Si deja de funcionar, cambiaron el packer,
   la clave XOR o el host. Esto es lo que evita enterarse por los usuarios.
5. `curl -I` al master.m3u8 de esos 3: 200 y `application/vnd.apple.mpegurl`.
6. Espacio en disco del VPS y RAM disponible (el margen es de 3 GB).
7. Rotar el reporte a `cron/output/<job_id>/`.

### 6.2 Mantenimiento semanal

- Comprobar que las páginas nuevas están entrando en el índice (Search Console API o conteo de
  `site:`).
- Revisar `streams` con `fail_count >= 3` y purgar servidores muertos.
- Verificar que el sitemap no supere los límites (50k URLs / 50 MB por archivo) y que el índice
  resuelva.
- Actualizar `httpx`/dependencias y redesplegar en ventana de baja.

### 6.3 Lo que NO automatizo

Cambios de red de ads, respuesta a takedowns y decisiones de dominio. Eso lo decides tú; el cron
solo te avisa cuando pasa algo que lo requiere.

---

## 7. Costes mensuales

El VPS ya está pagado y en uso: el coste marginal del stack es ~0. Lo que sí suma:

| Concepto | Estimado |
|---|---|
| RAM adicional en el VPS actual | $0 (cabe, con 3 GB de margen) |
| Segundo VPS, si se añade headless browser | $15–25/mes |
| Cloudflare | $0 (free) hasta volumen alto |
| Ancho de banda de video | **$0** (hotlink al CDN) |
| Dominios (20–30/año, churn) | $20–30/mes |
| **Coste real mes 1–3** | **~$20–30** (solo dominios) |

El coste dominante no es infraestructura, es el churn de dominios y el tiempo. Si en algún momento
decides proxear el video en lugar de hotlinkear, el ancho de banda pasa a ser la partida principal
(1.000 concurrentes × 1,5 Mbps ≈ 1,5 Gbps) y multiplica el coste por diez.

---

## 8. Decisiones que hay que tomar antes de la fase 1

Estas no se pueden posponer, porque condicionan el despliegue:

1. **Jurisdicción del VPS y del registrador.** Determina cómo te llegan los takedowns y con qué
   agilidad te cierran. Es una decisión de negocio, no técnica.
2. **Dominio inicial y plan de churn.** Cuántos compras ahora, con qué registrador, y si el
   registro va a nombre de una entidad o de un tercero.
3. **Hotlink vs proxy de video.** Recomendado hotlink. Si algún día quieres ocultar la fuente,
   es un cambio de arquitectura, no un ajuste.
4. **Red de ads.** Afecta al markup del player, así que hay que decidirla antes de la fase 2.
5. **Proxear los embeds de terceros o no.** Recomendado no: son la pieza más frágil y el host
   propio cubre lo mismo.

---

## 9. Lo que puede hundir el proyecto

Por orden de probabilidad real:

1. **El dominio se quema rápido y no tienes churn automatizado.** Es el fallo más común. La
   arquitectura multi-dominio tiene que estar desde la fase 1 aunque solo uses uno.
2. **Se cae la infraestructura de cuevana** (host `.lol` o la clave XOR). Sin plan B, te quedas sin
   fuentes de un día para otro. Mitigación: resolver también los embeds de terceros como respaldo,
   bajo demanda y con navegador headless solo para esos casos.
3. **El player falla y el usuario se va.** Un `m3u8` caducado servido sin reintento es un rebote.
   La resolución on-demand con fallback no es opcional.
4. **La red de ads te banea y te quedas sin ingresos un mes.** Ten dos.
5. **No indexas.** Sin enlazado interno y sin sitemaps, Google no crawlea 17k páginas.

---

## 10. Primera semana, en concreto

```
Día 1   Dockerfile + docker-compose + schema. Servicio en Coolify con db/redis/web/worker
        arrancando vacío (deploy gris con /healthz verde antes de escribir lógica)
Día 2   crawler de catálogo -> 938 páginas en la DB
Día 3   resolver (token -> file_id -> embed -> unpack -> m3u8) + backfill 100 títulos + scheduler
Día 4   FastAPI + Jinja2, /pelicula/{slug}, player hls.js
Día 5   selector de servidores/idiomas + fallback + resolver on-demand
Día 6   /serie, temporadas, episodios, paginado. Backfill de los 17k en background
Día 7   sitemaps + JSON-LD + enlazado interno + cron de supervisión + dominio y Cloudflare
```

El día 1 es deploy primero y lógica después. Montar el esqueleto en Coolify con un `/healthz` que
responde 200 y los cuatro contenedores hablando entre sí cuesta medio día y elimina la clase de
problema más caro de diagnosticar: que el código esté bien y el stack no arranque. Todo lo que
venga después se despliega en un entorno que ya se sabe que funciona.

Al final del día 7 hay un sitio que un humano puede usar y Google puede crawlear. Todo lo demás es
iteración sobre eso.

---

## 11. CORRECCIÓN CRÍTICA (2026-10-18, verificada en producción)

El CDN **ata la firma a la IP que acuña el token**. Medido:

```
master    desde el VPS (misma IP que resolvió)   -> 200
variante  desde el VPS                            -> 200  (174 KB)
segmento  desde el VPS                            -> 200  (688 KB MPEG-TS)
mismo master desde otra IP (mi máquina)           -> 403
mismo master desde otra IP, con Referer correcto  -> 403
```

Consecuencia: **el hotlink no funciona.** No puedes resolver el m3u8 en el
servidor y dárselo al navegador del usuario, porque el usuario tiene otra IP.

Esto invalida la estimación de "ancho de banda $0" del apartado de costes.
Los dos modos reales son:

| Modo | Cómo funciona | Coste | Control de ads |
|---|---|---|---|
| `PLAYBACK_MODE=iframe` (actual) | el usuario carga el player original; **su** IP acuña el token | $0 | ninguno, son sus ads |
| `PLAYBACK_MODE=proxy` | tu servidor resuelve **y** sirve los segmentos, misma IP | 1,5 Mbps por espectador concurrente | total |

El proxy multiregión no es trivial: el nodo que resuelve y el nodo que sirve
el segmento tienen que compartir IP de salida, así que no vale resolver en un
sitio y servir desde un CDN cualquiera. Hay que proxear con el mismo egress
(o usar un CDN que preserve la IP de origen, tipo Tunnels, y aun así con
cuidado).

Estado en producción hoy: `iframe`, que es también lo que hace cuevana.
El trabajo del packer/XOR queda como base para el modo proxy, no como
requisito para que el sitio funcione.
