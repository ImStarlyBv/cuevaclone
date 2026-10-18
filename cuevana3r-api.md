# cuevana3r.pro — recon de la "API"

Fecha: 2026-10-18. Recon hecho con Chrome real (chrome-bridge) + `requests`.

## TL;DR

**No existe una API JSON.** El sitio es SSR (PHP detrás de Cloudflare): el catálogo y las
fuentes de video viajan **embebidos en el HTML**. No hay `/api`, `/wp-json`, `/ajax`, ni XHR de
catálogo en ningún punto — verificado con 20+ sondas y con el panel Network del navegador.

Hay tres superficies y una sola capa de cifrado:

| Qué | Dónde | Cómo se obtiene |
|---|---|---|
| Catálogo | `/peliculas?page=N`, `/series?page=N` | parseo de HTML |
| Búsqueda | `/explorar?s=<q>` | parseo de HTML |
| Fuentes por título | `/pelicula/<slug>`, `/serie/<slug>/episodio-NxM` | `data-server` en el HTML + decodificación local |

Lo importante: **el player propio no hace ninguna request para resolver la fuente**. El token es
un cifrado simétrico con clave hardcodeada en el HTML del shell del player. Se descifra offline.
No hace falta navegador, ni Frida, ni interceptar tráfico para la capa de proveedores.

---

## 1. Arquitectura observada

- Cloudflare delante (`server: cloudflare`, rocket-loader, `cf-cache-status: HIT` en el player).
- Render SSR: home = 144 KB de HTML con todo el catálogo y las carátulas.
- Carátulas desde TMDB (`image.tmdb.org/t/p/w200|w1280`).
- Analytics: GA4 `G-BQPNWTX0GP` + Cloudflare Insights.
- Publicidad: `bh.dingerepilobe.com`, `yippie.violtantlevroom.cfd`, `secutorlunts.cfd`,
  `fc.tmhwidens.com`, `ei.wrawcowry.com`. Se carga desde `/js/popa.js` y desde el shell del player
  cuando llega `?pops=true`.
- Host del player propio: **`tungtungsahur.cuevana3r.pro`**.
- `robots.txt` = `User-Agent: *` / `Allow: /` (sin restricciones). No hay `sitemap.xml`.
- El shell del player trae `noindex,nofollow,noarchive` + `disable-devtool` (anti DevTools,
  bloquea F12, selección y copiado). Irrelevante para scraping: el payload está en el HTML.

---

## 2. Endpoints reales

Todas son `GET`, sin auth, sin cookies, sin tokens de sesión.

### 2.1 Catálogo

```
GET https://cuevana3r.pro/peliculas?page=N      # 18 items por página
GET https://cuevana3r.pro/series?page=N         # 18 items por página
GET https://cuevana3r.pro/episodios/recientes   # últimos episodios publicados
```

- Paginación con `?page=N`. `?pagina=N` también responde 200 pero **ignora el parámetro** y
  devuelve la página 1 — solo `page` funciona.
- Última página con contenido en películas: **938** → ~16.900 títulos. Las páginas > 938
  devuelven 200 con HTML válido pero sin items (no 404).
- Series: paginación válida más allá de la página 150; vacía antes de la 300.

Estructura de cada item:

```html
<div class="movie-item">
  <a href="https://cuevana3r.pro/pelicula/<slug>" data-instant>
    <div class="item-picture"><figure>
      <img src="https://image.tmdb.org/t/p/w200/<hash>.jpg" class="poster" alt="película <TÍTULO>">
      <span class="year text-center">2026</span>
    </figure></div>
    <div class="item-detail"><p><TÍTULO></p></div>
  </a>
</div>
```

Slug estable, sin ID numérico. El `tmdb_id` (que sí se usa en los embeds) **no** está en la grilla;
hay que entrar al detalle.

### 2.2 Búsqueda

```
GET https://cuevana3r.pro/explorar?s=<query>&page=N
```

- El parámetro es **`s`**, no `q`. El `<form id="search-form">` hace GET a `/explorar` con
  `<input name=s>`.
- `/explorar` sin `s` o con `s=` vacío → 404.
- Devuelve películas **y** series mezcladas, con el mismo markup de item.
- Paginación con `&page=N` (confirmado: página 2 trae títulos distintos).

### 2.3 Detalle (metadata + fuentes)

```
GET https://cuevana3r.pro/pelicula/<slug>
GET https://cuevana3r.pro/serie/<slug>                    # temporadas
GET https://cuevana3r.pro/serie/<slug>/temporada-N        # episodios
GET https://cuevana3r.pro/serie/<slug>/episodio-NxM       # fuentes del episodio
```

Metadata disponible: `ld+json` con `@type: Movie|TVSeries` (name, description, actor, aggregate
rating), `<title>` con `(AÑO)`, bloque `Año de estreno</h2><p>YYYY</p>`, sinopsis, reparto, y
relacionados. El `tmdb_id` solo aparece dentro de las URLs de los embeds.

---

## 3. Fuentes: `data-server`

El bloque de servidores va agrupado por idioma. Cada grupo es un `<li class="tab-video-item">`
con un flag (`lat.png` / `cas.png` / `sub.png`) y dentro una lista de `<li data-server="...">`:

```html
<li class="tab-video-item">
  <img class="tab-item-image" src=https://cuevana3r.pro/image/lat.png>
  ...
  <ul>
    <li role="presentation" data-server="https://tungtungsahur.cuevana3r.pro/?token=1D1ZdAEgBWlxOV0oK">
      <span>Servidor Hyper</span><span>Reproducir</span>
    </li>
    <li role="presentation" data-server="https://tungtungsahur.cuevana3r.pro/?token=3BwJDCllQGx0UAF5L">
      <span>Servidor Nebula</span><span>Reproducir</span>
    </li>
  </ul>
</li>
```

Todos los `data-server` apuntan al **mismo host** (`tungtungsahur.cuevana3r.pro`) y traen uno de
dos parámetros. Esa es toda la "API" de proveedores.

### 3.1 `?v=<base64>` — embed externo directo

Base64 estándar del URL final. Sin cifrado.

| Servidor | Destino | Formato |
|---|---|---|
| Goldmember | `vsembed.ru` | `/embed/movie?tmdb=<id>&ds_lang=es` |
| Death Star | `vidlink.pro` | `/movie/<id>?sub_label=Spanish&...&player=jw` |
| Trinity | `player.videasy.net` | `/movie/<id>` |
| Mahoutokoro | `vidapi.xyz` | `/embed/movie/<id>` |

Los cuatro resuelven por `tmdb_id` — el mismo id para todos. Se pueden usar directamente como
`src` de un iframe. Ventaja: cero mantenimiento. Desventaja: el HTML de esos players es de
terceros, no controlas ni ads ni disponibilidad.

### 3.2 `?token=<token>` — player propio (Hyper / Nebula)

El token **no** se valida contra el servidor. El shell del player (HTML, 11 KB) trae el algoritmo
inline:

```js
if (params.has('token')) {
    const token = params.get('token');
    const servers = {
        1: 'https://lkhjerbhye3wjkhoduh5xiczuvd.lol/v/',
        2: 'https://filemoon.sx/e/',
        3: 'https://lkhjerbhye3wjkhodvh5xlczuvd.lol/e/',
        4: 'https://dood.li/e/'
    };
    const key = 'a45f04ce'+'-'+'2394'+'-'+'47c3'+'-'+'b718'+'-'+'0ecd97ce51d6';
    const decoded = atob(token.slice(1));
    let decrypted = '';
    for (let i = 0; i < decoded.length; i++) {
        decrypted += String.fromCharCode(decoded.charCodeAt(i) ^ key.charCodeAt(i % key.length));
    }
    videoUrl = servers[token[0]] + decrypted;
    startCountdown();
}
```

Formato: `token[0]` = id de servidor (1–4), `token[1:]` = base64 del file-id cifrado con XOR
contra la clave `a45f04ce-2394-47c3-b718-0ecd97ce51d6` (36 bytes, se repite en ciclo).

Equivalente en Python:

```python
import base64
KEY = b"a45f04ce-2394-47c3-b718-0ecd97ce51d6"
SERVERS = {
    "1": "https://lkhjerbhye3wjkhoduh5xiczuvd.lol/v/",
    "2": "https://filemoon.sx/e/",
    "3": "https://lkhjerbhye3wjkhodvh5xlczuvd.lol/e/",
    "4": "https://dood.li/e/",
}

def decode_token(tok):
    raw = tok[1:] + "=" * (-len(tok[1:]) % 4)
    data = base64.b64decode(raw)
    fid = bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(data)).decode()
    return SERVERS[tok[0]] + fid
```

Verificado contra 7 tokens reales (6 de película + 1 de episodio). Ejemplos:

```
1D1ZdAEgBWlxOV0oK -> https://lkhjerbhye3wjkhoduh5xiczuvd.lol/v/nbhfx599cey3
3BwJDCllQGx0UAF5L -> https://lkhjerbhye3wjkhodvh5xlczuvd.lol/e/f6vlidxx92mr
3EF4MV1FfARRJSF4N -> https://lkhjerbhye3wjkhodvh5xlczuvd.lol/e/qj91akbqdzm4   (S3E1 de La casa del dragón)
```

Los tres devuelven HTTP 200.

Notas del esquema:

- El token es **del contenido, no de la sesión**. El mismo token sirve desde cualquier IP, sin
  cookies. Es un identificador ofuscado, no un mecanismo de autorización.
- `filemoon.sx` y `dood.li` son hosts de video estándar (embed con su propio JS). El `.lol`
  (`lkhjerbhye3wjkhoduh5xiczuvd.lol`) es infraestructura propia de cuevana, mismo player para
  `/v/` y `/e/`.
- El shell del player además reescribe `vidsrc.pm` → `vidsrc.me` si aparece en la URL.

---

## 4. Límite de tasa

- 12 requests secuenciales a `/peliculas?page=N` en 0,7 s → todos 200. Sin rate limit a ese ritmo.
- Un burst agresivo (binary search de ~30 requests seguidos) → **429** en todo el sitio, incluido
  el catálogo. `Retry-After` ausente.
- La ventana se libera sola: tras 75 s volvió a 200.
- Recomendación para el scraper: 2–3 req/s, backoff exponencial en 429 (empezando en 60 s), y
  rotar salida por proxy si hay que hacer un backfill de las 938 páginas.

---

## 5. Cliente

`cuevana3r_client.py` (mismo directorio) implementa todo lo anterior. Solo depende de `requests`.

```bash
PY="$LOCALAPPDATA/hermes/hermes-agent/venv/Scripts/python.exe"

"$PY" cuevana3r_client.py catalog 1              # /peliculas?page=1
"$PY" cuevana3r_client.py catalog 1 series       # /series?page=1
"$PY" cuevana3r_client.py search "spider"
"$PY" cuevana3r_client.py movie spider-man-un-nuevo-dia
"$PY" cuevana3r_client.py series la-casa-del-dragon 3
"$PY" cuevana3r_client.py episode la-casa-del-dragon 3 1
```

Función clave expuesta al resto del pipeline:

```python
from cuevana3r_client import movie
data = movie("spider-man-un-nuevo-dia")
# data["sources"] -> [{"language": "Latino", "servers": [{"name":..., "type":"token",
#                       "token":..., "url":"https://.../v/nbhfx599cey3", "file_id":...}]}]
```

`type` es `token` (URL ya resuelta y usable como iframe) o `embed` (URL de terceros directa).
Ninguna requiere red adicional para resolverse.

### Resultado de las pruebas

```
movie("spider-man-un-nuevo-dia")
  [Latino     ] Servidor Hyper        https://lkhjerbhye3wjkhoduh5xiczuvd.lol/v/nbhfx599cey3
  [Latino     ] Servidor Nebula       https://lkhjerbhye3wjkhodvh5xlczuvd.lol/e/f6vlidxx92mr
  [Castellano ] Servidor Hyper        https://lkhjerbhye3wjkhoduh5xiczuvd.lol/v/4o9c5oczazs0
  [Castellano ] Servidor Nebula       https://lkhjerbhye3wjkhodvh5xlczuvd.lol/e/asz1sh556qih
  [Subtitulado] Servidor Hyper        https://lkhjerbhye3wjkhoduh5xiczuvd.lol/v/tglcfs7j0e3f
  [Subtitulado] Servidor Nebula       https://lkhjerbhye3wjkhodvh5xlczuvd.lol/e/ea5twwhrqapa
  [Subtitulado] Servidor Goldmember   https://vsembed.ru/embed/movie?tmdb=969681&ds_lang=es
  [Subtitulado] Servidor Death Star   https://vidlink.pro/movie/969681?sub_label=Spanish&...
  [Subtitulado] Servidor Trinity      https://player.videasy.net/movie/969681
  [Subtitulado] Servidor Mahoutokoro  https://vidapi.xyz/embed/movie/969681
```

Catálogo, búsqueda, temporadas y episodios: verificados. 12 páginas de catálogo seguidas: 200.

---

## 6. Lo que queda por resolver (siguiente capa)

1. **m3u8 final.** Las URLs de arriba son páginas de embed, no streams. `filemoon.sx`, `dood.li` y
   el `.lol` propio entregan el HLS tras ejecutar su JS (ofuscado + challenge). Para hotlinkear
   directo hay que resolver esa capa — o quedarse en iframe, que es lo que hace cuevana.
2. **Proxear el player.** Tocar `tungtungsahur` como host de confianza es peor idea que reusar la
   URL resuelta: un cambio en el host o en la clave rompe el cliente entero. La clave está
   hardcodeada en HTML público, así que puede rotar; conviene detectar la rotación parseando el
   shell en vez de asumir la constante.
3. **Backfill.** 938 páginas × 18 = ~16.9k películas + series. A 2 req/s son ~8 minutos de
   catálogo, más 1 request por título para las fuentes: ~17k requests → ~2,5 h con rate limit sano.
4. **Frescura.** El home trae "Última actualización: 18/10/2026" y `/episodios/recientes` da el
   delta diario. Ese endpoint es el que conviene pollear para el pipeline incremental.
5. **Campos que faltan:** `tmdb_id` no está en la grilla, solo en los embeds. Si lo necesitas como
   clave canónica, sale de aquí:
   `re.search(r"tmdb=(\d+)", embed_url)` o del path de `vidlink/videasy`.


---

## 7. Capa m3u8 — resuelta (verificada end-to-end)

Probado sobre el servidor propio de cuevana (el `.lol`), que es el que importa.

### 7.1 Cadena completa

```
/pelicula/<slug>                         -> data-server="...?token=XYZ"
token[1:] base64 + XOR (clave)           -> file_id           p.ej. f6vlidxx92mr
GET https://<host>/e/<file_id>           -> HTML 15 KB, JW Player 8 + packer
unpack(eval(function(p,a,c,k,e,d)...))   -> source con el objeto links
links.hls2                               -> master.m3u8 firmado
```

El HTML del embed no tiene challenge, ni captcha, ni Cloudflare, ni necesita JS para entregar
el source: el packer está en un `<script>` inline y se desempaqueta offline.

### 7.2 El packer

Es el de Dean Edwards (base62), el mismo que usan miles de sitios desde 2006. Se desempaqueta
sustituyendo `eval(` por `console.log(` y ejecutando:

```python
src = open('packed.js').read().strip()
open('unpack.js','w').write('console.log(' + src[5:])
# node unpack.js > unpacked.js
```

9355 bytes de packer -> 10715 bytes de source legible. No hace falta ejecutar el player.

### 7.3 Lo que aparece dentro

```js
links = {
  "hls2": "https://jZdDKEFw9oEu0.premilkyway.com/hls2/01/15133/f6vlidxx92mr_n/master.m3u8
           ?t=wsjI8hZqQVFLZX-TtBvGibw_stfQXJb1hlLjgYP1shE&s=1792356703&e=129600
           &f=75667987&srv=z13jtbat10fahdh456&i=0.4&sp=500&...&asn=174",
  "hls3": "https://jZdDKEFw9oEu0.viatrixfashionhouse.sbs/.../master.txt",
  "hls4": "/stream/c0Wj5Bjx531bcDPcAN93Gw/kjhhiuahiuhgihdf/1792399903/75667987/master.m3u8"
}
jwplayer("vplayer").setup({sources:[{file: links.hls4||links.hls3||links.hls2, type:"hls"}], ...})
```

Tres rutas al mismo contenido:

| Campo | Forma | Estado medido |
|---|---|---|
| `hls2` | CDN firmado (`*.premilkyway.com`) | 200, `application/vnd.apple.mpegurl`, 618 B |
| `hls3` | CDN alterno (`*.viatrixfashionhouse.sbs`) | no probado, mismo token |
| `hls4` | proxy relativo del propio host (`/stream/...`) | 200, master de 160 B |

### 7.4 Semántica del token

| Param | Significado | Valor observado |
|---|---|---|
| `t` | token de firma | 40 chars, por archivo |
| `s` | timestamp de emisión | 1792356703 = 2026-10-18 20:51:43 UTC (≈ momento del fetch) |
| `e` | validez en segundos | 129600 = **36 h** |
| `f` | file_id numérico | 75667987 |
| `srv` / `p1` / `p2` | nodo de origen | z13jtbat10fahdh456 |
| `asn` | ASN del cliente | 174 (tu salida) |

`s` se genera **en el momento de servir el HTML del embed**, no es fijo por título. Consecuencia:
el m3u8 hay que sacarlo de un GET al embed cada vez que se refresque la cache, no se puede
guardar de forma permanente. Ventana útil: 36 h; cachear 6–12 h es lo prudente.

### 7.5 Playlist y segmentos

```
master.m3u8 -> 1 variante: 1728x720, 24 fps, avc1.640020, BANDWIDTH=1529181 (~1,5 Mbps)
            -> index-v1-a1.m3u8 (VOD, TARGETDURATION 10, MEDIA-SEQUENCE 1)
            -> seg-1-v1-a1.ts  ->  HTTP 200, 238.008 bytes, video/MP2T, sync 0x47
```

Verificado con `curl` puro. Sin `Referer` también responde 200 — el CDN no valida referer,
así que el segmento se puede hotlinkear desde tu propio player sin pagar ancho de banda.

Nota: el playlist en minúsculas (`jzddkefw9oeu0.premilkyway.com`) resuelve igual que el
original. El host no es case-sensitive.

### 7.6 Coste de resolución

Cero navegador. Por título: **1 request HTTP** (15 KB) + CPU despreciable. Comparado con la
alternativa headless (5–10 s y 300–800 MB de RAM por contexto), la diferencia es de dos órdenes
de magnitud.

Backfill completo (~17k títulos) a 2–3 req/s: ~2 h. Refresco cada 12 h: ~0,4 req/s. Irrelevante.

### 7.7 Qué se rompe

| Pieza | Estabilidad | Mitigación |
|---|---|---|
| HTML de `/pelicula/<slug>` | alta | ninguna necesaria |
| Clave XOR del player | media — está en HTML público, puede rotar | parsear el shell en vez de hardcodear |
| Formato del packer | alta (Dean Edwards, 20 años estable) | unpacker genérico, no específico del host |
| Nombre del objeto `links` | media | regex sobre `master\.m3u8` en el source desempaquetado, no sobre `links.hls2` |
| Dominio CDN (`premilkyway`, `viatrix...`) | **baja — rotan por diseño** | se leen del source cada vez; nunca hardcodear |
| file_id / disponibilidad del archivo | media — DMCA tumba archivos sueltos | fallback entre servidores del mismo título |
| Hosts de terceros (filemoon, dood, videasy...) | **baja** — SPA + challenge + anti-bot | ignorarlos; el host propio cubre todo |

Conclusión de mantenibilidad: **la capa m3u8 del host propio es la pieza más barata de todo el
stack**, más que el catálogo. Lo único que exige vigilancia es la rotación de la clave XOR y el
cambio de nombre del objeto `links` — ambos detectables con un test diario que falle ruidoso.

Lo que NO es mantenible son los embeds de terceros. Ahí sí hace falta navegador headless, y ahí
sí se rompe cada pocas semanas. La jugada es no depender de ellos.
