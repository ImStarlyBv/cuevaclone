"""Resolución de una fuente de video: token/embed -> master.m3u8.

Compartido por el worker (backfill y refresco) y por la web (resolución
on-demand cuando el usuario abre un título cuyo m3u8 ya caducó).

Los tres campos que importan del resultado:
  m3u8_url   URL absoluta reproducible con hls.js
  host       dominio del CDN (para diagnosticar, nunca se hardcodea)
  expires_at estimación de caducidad, a partir del parámetro `e=` de la firma
"""

import time
from urllib.parse import parse_qs, urlparse

import requests

from . import unpack as unpacker
from .client import UA, decode_server

# Un master.m3u8 firmado por cuevana lleva e=<segundos de validez>.
DEFAULT_TTL = 36 * 3600
TTL_SAFETY = 2 * 3600


class ResolveError(Exception):
    def __init__(self, step, message, http=None):
        super().__init__(message)
        self.step = step
        self.http = http


def _find_packed(html):
    """Localiza el bloque `eval(function(p,a,c,k,e,d){...})` con paréntesis balanceados."""
    marker = "eval(function(p,a,c,k,e,d)"
    i = html.find(marker)
    if i == -1:
        return None
    depth = 0
    for pos in range(i + 4, len(html)):
        ch = html[pos]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return html[i:pos + 1]
    return None


def _ttl_from(m3u8_url):
    """Saca la validez del parámetro e= de la firma, si está."""
    try:
        qs = parse_qs(urlparse(m3u8_url).query)
        if "e" in qs:
            return int(qs["e"][0])
    except Exception:
        pass
    return DEFAULT_TTL


def resolve(sess, source_url, timeout=30):
    """Resuelve una URL de `data-server` a una URL de master.m3u8.

    Lanza ResolveError con el paso que falló, para que el caller lo registre.
    """
    if not source_url:
        raise ResolveError("decode", "fuente vacía")

    try:
        decoded = decode_server(source_url)
        if isinstance(decoded, dict):
            decoded = decoded.get("url")
    except Exception as exc:
        raise ResolveError("decode", f"{type(exc).__name__}: {exc}")

    if not decoded or not str(decoded).startswith("http"):
        raise ResolveError("decode", f"URL inválida tras decodificar: {decoded!r}")

    try:
        r = sess.get(decoded, timeout=timeout, headers={"User-Agent": UA})
    except Exception as exc:
        raise ResolveError("embed_fetch", f"{type(exc).__name__}: {exc}")

    if r.status_code != 200 or len(r.text) < 2000:
        raise ResolveError(
            "embed_fetch", f"HTTP {r.status_code}, {len(r.text)} bytes", r.status_code
        )

    packed = _find_packed(r.text)
    if packed is None:
        raise ResolveError("unpack", "el player no trae bloque empaquetado")

    try:
        links = unpacker.extract_links(unpacker.unpack(packed))
    except Exception as exc:
        raise ResolveError("unpack", f"{type(exc).__name__}: {exc}")

    if not links:
        raise ResolveError("unpack", "el packer no expone campos hlsN")

    # Preferencia: hls2 (CDN firmado, hotlink directo) > hls3 > hls4 (proxy del host).
    raw = links.get("hls2") or links.get("hls3") or links.get("hls4")
    if not raw:
        raise ResolveError("unpack", f"sin URL utilizable en {sorted(links)}")

    if raw.startswith("/"):
        base = urlparse(decoded)
        raw = f"{base.scheme}://{base.netloc}{raw}"

    ttl = _ttl_from(raw)
    return {
        "m3u8_url": raw,
        "host": urlparse(raw).netloc,
        "expires_at": int(time.time()) + max(ttl - TTL_SAFETY, 600),
        "resolved_via": "packer",
    }


def check_master(sess, m3u8_url, timeout=20):
    """Comprueba que el master responde y es HLS. Devuelve (ok, http, variantes)."""
    try:
        r = sess.get(m3u8_url, timeout=timeout, headers={"User-Agent": UA})
        ok = r.status_code == 200 and "#EXTM3U" in r.text
        return ok, r.status_code, r.text.count("#EXT-X-STREAM-INF")
    except Exception:
        return False, None, 0
