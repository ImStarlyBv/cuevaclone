# cuevaclone

Scraper y plan de montaje para un sitio de streaming, basado en el recon de `cuevana3r.pro`.

## Contenido

| Archivo | Qué es |
|---|---|
| `cuevana3r-api.md` | Informe del recon: endpoints, esquema de tokens, capa m3u8. Todo verificado contra el sitio real. |
| `cuevana3r_client.py` | Cliente funcional: catálogo, búsqueda, detalle, series, episodios, decodificación de tokens. |
| `unpack.py` | Desempaquetador Dean Edwards en Python puro. Verificado byte a byte contra node. |
| `PLAN.md` | Plan de montaje: stack, esquema de datos, fases, despliegue en Coolify, mantenimiento. |

## Lo esencial del recon

No hay API JSON. El sitio es SSR y todo viaja en el HTML.

```
catálogo     GET /peliculas?page=N        18 por página, 938 páginas (~16.9k títulos)
             GET /series?page=N
búsqueda     GET /explorar?s=<query>      el parámetro es `s`
delta        GET /episodios/recientes
detalle      GET /pelicula/<slug>         fuentes en atributos data-server
```

Las fuentes por título vienen en dos formas:

- `?v=<base64>` → embed externo directo (vsembed, vidlink, videasy, vidapi), por tmdb id.
- `?token=<token>` → player propio. `token[0]` es el id de servidor, `token[1:]` es
  base64 de `file_id XOR <clave>`. La clave está en el HTML público del player.

El m3u8 final se obtiene del shell del player de dos formas: `?v=` decodifica directo, y el player
propio trae un packer Dean Edwards inline que se desempaqueta offline (ver `unpack.py`).

## Uso

```bash
python cuevana3r_client.py catalog 1
python cuevana3r_client.py search "spider"
python cuevana3r_client.py movie spider-man-un-nuevo-dia
python cuevana3r_client.py series la-casa-del-dragon 3
python cuevana3r_client.py episode la-casa-del-dragon 3 1
```

Cada comando imprime JSON. `movie` y `episode` devuelven las fuentes ya decodificadas a URL final.

## Mantenimiento

El scraper depende de una fuente que cambia. La estrategia está descrita en `PLAN.md` §6:
scheduler dentro del worker para la operación, cron de supervisión para el reporte, y un canario
que resuelve 3 títulos fijos de punta a punta en cada pasada. Si el canario falla, la fuente cambió
y hay que tocar el parser — no el sitio.

## Nota

Esto scrapea infraestructura de terceros y el contenido no es propio. Es tu decisión operarlo.
