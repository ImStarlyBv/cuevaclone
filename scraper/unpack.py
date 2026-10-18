"""
unpack.py — desempaquetador Dean Edwards en Python puro.

Verificado byte a byte contra la salida de node sobre el packer real de
lkhjerbhye3wjkhoduh5xiczuvd.lol (10.714 bytes, idénticos).

Por qué portado y no `node -e`: el backfill son ~17.000 títulos. Lanzar un
proceso node por título son 17.000 arranques de V8 (~80 ms cada uno = ~22 min
solo de overhead de proceso, más el consumo de RAM por fork). En Python es una
sola pasada de 500 sustituciones regex sobre ~6 KB: 34 ms medidos, ~10 min de
CPU repartidos a lo largo de un backfill de 3-5 h.

Gotchas que costaron tiempo y están resueltas aquí:

1. `\\b` es Unicode-aware en Python y no en JS. Con texto en español (á, ñ)
   el límite de palabra cae en otro sitio y el unpack sale corrupto.
   Solución: `flags=re.ASCII`.
2. El payload es un literal JS de comillas simples: `\\'` en el fuente es un
   escape, no un backslash literal. Hay que desescapar ANTES de procesar, o
   la salida conserva backslashes que node no produce.
3. El packer puede envolver la llamada como `eval(function(...){...}('...'))`
   o sin `eval(`. El corte del payload se hace desde el final (`.split('|')` y
   el par `,<a>,<c>`), no con una regex codiciosa desde el principio: el
   payload contiene `,'` y comillas sueltas que rompen un match ingenuo.
"""

import re

_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SUFFIX = "'.split('|'))"
_MARKER = "return p}('"


def _to_base(num: int, base: int) -> str:
    """Equivalente a la función e(c) del packer de Dean Edwards."""
    if num == 0:
        return "0"
    out = []
    while num > 0:
        r = num % base
        out.append(chr(r + 29) if r > 35 else _B62[r])
        num //= base
    return "".join(reversed(out))


def _decrypt(p: str, a: int, c: int, k: list) -> str:
    for cc in range(c - 1, -1, -1):
        if cc < len(k) and k[cc]:
            p = re.sub(
                r"\b" + re.escape(_to_base(cc, a)) + r"\b",
                lambda m, v=k[cc]: v,
                p,
                flags=re.ASCII,
            )
    return p


def _strip_js_escapes(p: str) -> str:
    return re.sub(r"\\(.)", r"\1", p, flags=re.S)


def unpack(source: str) -> str:
    """Desempaqueta un bloque `eval(function(p,a,c,k,e,d){...}(...))`.

    Lanza ValueError si la forma no coincide con la esperada, para que el
    caller pueda marcar el stream como error en vez de guardar basura.
    """
    s = source.strip()
    if s.startswith("eval(") and s.endswith(")"):
        s = s[5:-1]

    if not s.endswith(_SUFFIX):
        raise ValueError("no termina en '.split('|'))': no parece un packer")
    body = s[: -len(_SUFFIX)]

    i = body.rindex(",'")
    keywords = body[i + 2:].split("|")
    head = body[:i]

    parts = head.rsplit(",", 2)
    if len(parts) != 3:
        raise ValueError("no encuentro el par <base>,<count>")
    try:
        a, c = int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise ValueError("base/count no numéricos") from exc

    if _MARKER not in head:
        raise ValueError("no encuentro el marcador 'return p}('")
    j = head.rindex(_MARKER) + len(_MARKER)
    packed = head[j: -len("," + parts[1] + "," + parts[2]) - 1]

    return _decrypt(_strip_js_escapes(packed), a, c, keywords)


def extract_links(unpacked: str) -> dict:
    """Saca los campos hlsN del objeto `links` del source desempaquetado.

    Se busca `"hlsN":"<url>"` en todo el texto en vez de navegar al objeto
    `links`: si cuevana renombra la variable el día de mañana, esto sigue
    funcionando.
    """
    found = {}
    for name, url in re.findall(r'"(hls\d)"\s*:\s*"([^"]+)"', unpacked):
        found[name] = url
    return found
