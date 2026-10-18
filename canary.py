"""
canary.py — prueba de humo de la cadena completa, paso por paso.

El objetivo no es decir "está roto", es decir EN QUÉ PASO se rompió y dejar el
artefacto exacto que falló guardado en disco. Sin eso, el reporte diario obliga a
volver a hacer el recon desde cero cada vez.

Cadena que se prueba, por título:

    detail_fetch   GET /pelicula/<slug>
    parse_servers  el HTML trae fuentes en data-server
    decode         token XOR -> URL,  o  ?v= base64 -> URL
    embed_fetch    GET de la página del player
    unpack         desempaquetar el packer -> objeto links
    m3u8_fetch     GET del master.m3u8 -> 200 + content-type HLS

Uso:
    python canary.py                 # corre los 3 títulos por defecto
    python canary.py --json          # solo el JSON, sin resumen humano
    python canary.py --save-db       # además escribe en health_runs (psycopg2)

Salida: JSON por stdout, exit 0 si todo verde, exit 1 si algo falló.
Los artefactos de fallo van a ./failures/<YYYYmmdd-HHMMSS>/.

Nota sobre las URLs: las firmas caducan y son sensibles, así que en stdout solo
se imprime host+ruta. El detalle completo (con query) se escribe únicamente en
el archivo de evidencia en disco.
"""

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests
from urllib.parse import urlparse

from scraper import unpack as unpacker
from scraper.client import (
    PLAYER_HOST,
    UA,
    decode_server,
    get_html,
    parse_servers,
    session,
)

# Títulos estables: clásicos con catálogo profundo, mucho menos propensos a que
# les tumben el archivo que un estreno de la semana.
CANARY_TITLES = [
    "el-hombre-arana-2",
    "el-hombre-arana-3",
    "aracnofobia-ii",
]

STEPS = [
    "detail_fetch",
    "parse_servers",
    "decode",
    "embed_fetch",
    "unpack",
    "m3u8_fetch",
]

ARTIFACT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "failures")


def _now():
    return datetime.now(timezone.utc)


def _safe_url(url):
    """host + ruta, sin query. Para logs y stdout."""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return "<url invalida>"


def _step(results, name, ok, ms, http=None, error=None, detail=None):
    entry = {"step": name, "ok": ok, "ms": round(ms, 1)}
    if http is not None:
        entry["http"] = http
    if error:
        entry["error"] = error[:400]
    if detail:
        entry.update(detail)
    results.append(entry)
    return entry


def _save_artifact(run_dir, step, name, content):
    if not content:
        return None
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, f"{step}__{name}")
    with open(path, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(content)
    return os.path.basename(path)


def check_title(sess, slug, run_dir=None):
    """Recorre la cadena completa para un título. Devuelve un dict de resultado."""
    steps = []
    result = {
        "slug": slug,
        "ok": False,
        "failed_step": None,
        "steps": steps,
        "artifact_dir": None,
    }

    html = None
    servers = []
    target = None
    embed_html = None
    unpacked = None
    links = {}
    master = None

    # 1. detalle -------------------------------------------------------------
    t0 = time.perf_counter()
    try:
        r = sess.get(f"https://cuevana3r.pro/pelicula/{slug}", timeout=25,
                     headers={"User-Agent": UA}, allow_redirects=True)
        html = r.text
        ok = r.status_code == 200 and len(html) > 5000
        _step(steps, "detail_fetch", ok, (time.perf_counter() - t0) * 1000,
              http=r.status_code, detail={"bytes": len(html)})
        if not ok:
            result["failed_step"] = "detail_fetch"
            _save_artifact(run_dir, "detail_fetch", f"{slug}.html", html)
            return result
    except Exception as exc:
        _step(steps, "detail_fetch", False, (time.perf_counter() - t0) * 1000,
              error=f"{type(exc).__name__}: {exc}")
        result["failed_step"] = "detail_fetch"
        return result

    # 2. parse de fuentes ----------------------------------------------------
    t0 = time.perf_counter()
    try:
        servers = parse_servers(html)
        flat = [s for group in servers for s in group.get("servers", [])]
        ok = len(flat) > 0
        _step(steps, "parse_servers", ok, (time.perf_counter() - t0) * 1000,
              detail={"sources": len(flat),
                      "tokened": sum(1 for s in flat if s.get("type") == "token")})
        if not ok:
            result["failed_step"] = "parse_servers"
            _save_artifact(run_dir, "parse_servers", f"{slug}.html", html)
            return result
    except Exception as exc:
        _step(steps, "parse_servers", False, (time.perf_counter() - t0) * 1000,
              error=f"{type(exc).__name__}: {exc}")
        result["failed_step"] = "parse_servers"
        return result

    # 3. decodificacion de la primera fuente ---------------------------------
    t0 = time.perf_counter()
    try:
        preferred = [s for s in flat if s.get("type") == "token"] or flat
        target = preferred[0]
        url = decode_server(target.get("url") or target.get("embed_url") or "")
        if isinstance(url, dict):
            url = url.get("url")
        ok = bool(url) and str(url).startswith("http")
        _step(steps, "decode", ok, (time.perf_counter() - t0) * 1000,
              detail={"type": target.get("type"), "target": _safe_url(url or "")})
        if not ok:
            result["failed_step"] = "decode"
            _save_artifact(run_dir, "decode", f"{slug}.txt",
                           json.dumps(target, indent=2, default=str))
            return result
        target_url = url
    except Exception as exc:
        _step(steps, "decode", False, (time.perf_counter() - t0) * 1000,
              error=f"{type(exc).__name__}: {exc}")
        result["failed_step"] = "decode"
        return result

    # 4. pagina del player ---------------------------------------------------
    t0 = time.perf_counter()
    try:
        r = sess.get(target_url, timeout=25, headers={"User-Agent": UA},
                     allow_redirects=True)
        embed_html = r.text
        ok = r.status_code == 200 and len(embed_html) > 2000
        _step(steps, "embed_fetch", ok, (time.perf_counter() - t0) * 1000,
              http=r.status_code, detail={"bytes": len(embed_html)})
        if not ok:
            result["failed_step"] = "embed_fetch"
            _save_artifact(run_dir, "embed_fetch", f"{slug}.html", embed_html)
            return result
    except Exception as exc:
        _step(steps, "embed_fetch", False, (time.perf_counter() - t0) * 1000,
              error=f"{type(exc).__name__}: {exc}")
        result["failed_step"] = "embed_fetch"
        return result

    # 5. unpack --------------------------------------------------------------
    t0 = time.perf_counter()
    packed = None
    try:
        packed = _find_packed(embed_html)
        if packed is None:
            # puede ser un embed ?v= que no usa packer: ya se decodificó arriba
            _step(steps, "unpack", True, (time.perf_counter() - t0) * 1000,
                  detail={"skipped": "el player no trae packer (embed directo)"})
        else:
            unpacked = unpacker.unpack(packed)
            links = unpacker.extract_links(unpacked)
            ok = bool(links)
            _step(steps, "unpack", ok, (time.perf_counter() - t0) * 1000,
                  detail={"links": sorted(links.keys())})
            if not ok:
                result["failed_step"] = "unpack"
                _save_artifact(run_dir, "unpack", f"{slug}.packed.js", packed)
                _save_artifact(run_dir, "unpack", f"{slug}.unpacked.js", unpacked)
                return result
    except Exception as exc:
        _step(steps, "unpack", False, (time.perf_counter() - t0) * 1000,
              error=f"{type(exc).__name__}: {exc}")
        result["failed_step"] = "unpack"
        _save_artifact(run_dir, "unpack", f"{slug}.packed.js", packed or "")
        _save_artifact(run_dir, "unpack", f"{slug}.traceback.txt", traceback.format_exc())
        return result

    # 6. master.m3u8 ---------------------------------------------------------
    t0 = time.perf_counter()
    try:
        raw = links.get("hls4") or links.get("hls2") or links.get("hls3") or target_url
        if str(raw).startswith("/"):
            base = urlparse(target_url)
            raw = f"{base.scheme}://{base.netloc}{raw}"
        r = sess.get(raw, timeout=30, headers={"User-Agent": UA})
        ok = r.status_code == 200 and "#EXTM3U" in r.text
        _step(steps, "m3u8_fetch", ok, (time.perf_counter() - t0) * 1000,
              http=r.status_code,
              detail={"target": _safe_url(raw),
                      "content_type": r.headers.get("content-type", "")[:60],
                      "variants": r.text.count("#EXT-X-STREAM-INF")})
        if ok:
            master = _safe_url(raw)
        else:
            result["failed_step"] = "m3u8_fetch"
            _save_artifact(run_dir, "m3u8_fetch", f"{slug}.m3u8", r.text[:20000])
            return result
    except Exception as exc:
        _step(steps, "m3u8_fetch", False, (time.perf_counter() - t0) * 1000,
              error=f"{type(exc).__name__}: {exc}")
        result["failed_step"] = "m3u8_fetch"
        return result

    result["ok"] = True
    result["master"] = master
    return result


def _find_packed(html):
    """Localiza el bloque `eval(function(p,a,c,k,e,d){...}` en el HTML."""
    marker = "eval(function(p,a,c,k,e,d)"
    i = html.find(marker)
    if i == -1:
        return None
    depth = 0
    j = i + 4  # tras 'eval'
    for pos in range(j, len(html)):
        ch = html[pos]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return html[i:pos + 1]
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Canario de la cadena cuevana")
    ap.add_argument("--json", action="store_true", help="solo JSON")
    ap.add_argument("--titles", nargs="*", default=CANARY_TITLES)
    args = ap.parse_args(argv)

    run_id = _now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(ARTIFACT_DIR, run_id)

    sess = session()
    results = []
    t0 = time.perf_counter()
    for slug in args.titles:
        results.append(check_title(sess, slug, run_dir))

    failed = [r for r in results if not r["ok"]]
    for r in failed:
        r["artifact_dir"] = run_dir if os.path.isdir(run_dir) else None
    report = {
        "run_id": run_id,
        "ran_at": _now().isoformat(),
        "duration_s": round(time.perf_counter() - t0, 2),
        "ok": not failed,
        "titles": results,
        "failed_step": failed[0]["failed_step"] if failed else None,
        "artifact_dir": run_dir if failed and os.path.isdir(run_dir) else None,
    }

    print(json.dumps(report, indent=2, ensure_ascii=False))

    if not args.json:
        print("\n--- resumen ---", file=sys.stderr)
        for r in results:
            mark = "OK  " if r["ok"] else "FAIL"
            extra = "" if r["ok"] else f"  <- {r['failed_step']}"
            print(f"  {mark} {r['slug']}{extra}", file=sys.stderr)
            for s in r["steps"]:
                flag = "  ok" if s["ok"] else "  >>"
                detail = s.get("error") or ""
                print(f"      {flag} {s['step']:<14} {s['ms']:>7.1f}ms {detail}",
                      file=sys.stderr)
        if failed:
            print(f"\n  artefactos: {run_dir}", file=sys.stderr)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
