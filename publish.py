#!/usr/bin/env python3
"""
Publicación diaria de SOSING S.A.S. en Facebook e Instagram.

Corre en GitHub Actions (el único lugar de esta arquitectura con salida a
graph.facebook.com) y habla directamente con la API de Meta. No depende de
Zapier ni de ningún intermediario de pago.

Entradas:
    calendario.csv        dia,fecha,tema,fb,ig,hashtags,cta,url_imagen
    publicados.log        bitácora; una línea por día ya publicado
    META_TOKEN (secreto)  token de acceso de la página

Salidas:
    publicados.log        con la línea del día agregada
    código de salida      0 si publicó o si no había nada que publicar,
                          1 si falló (GitHub avisa por correo al dueño del repo)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = "https://graph.facebook.com/v21.0"
PAGE_ID = "233968186810864"        # Página de Facebook de SOSING
IG_USER_ID = "17841401014777842"   # Cuenta @sosing_sas

CALENDARIO = "calendario.csv"
BITACORA = "publicados.log"

TOKEN = os.environ.get("META_TOKEN", "").strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def post(path: str, params: dict) -> dict:
    """POST a la Graph API. Devuelve el JSON o lanza RuntimeError con el mensaje de Meta."""
    params = dict(params)
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{path}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode(errors="replace")
        try:
            err = json.loads(cuerpo)["error"]
            detalle = f'{err.get("message")} (tipo {err.get("type")}, código {err.get("code")})'
        except Exception:
            detalle = cuerpo[:400]
        raise RuntimeError(f"Meta rechazó POST /{path}: {detalle}") from None


def get(path: str, params: dict) -> dict:
    params = dict(params)
    params["access_token"] = TOKEN
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def texto(fila: dict, campo: str) -> str:
    """Arma el texto final: cuerpo + CTA + hashtags, separados por línea en blanco."""
    partes = [fila.get(campo, "").strip()]
    for extra in ("cta", "hashtags"):
        v = (fila.get(extra) or "").strip()
        if v:
            partes.append(v)
    return "\n\n".join(p for p in partes if p)


def publicar_facebook(fila: dict) -> str:
    img = (fila.get("url_imagen") or "").strip()
    msg = texto(fila, "fb")
    if img:
        try:
            r = post(f"{PAGE_ID}/photos", {"url": img, "caption": msg})
            return str(r.get("post_id") or r.get("id"))
        except RuntimeError as e:
            # Mejor solo texto que nada: es la regla del sistema desde agosto.
            log(f"  Facebook con imagen falló ({e}); reintento solo texto.")
    r = post(f"{PAGE_ID}/feed", {"message": msg})
    return str(r.get("id"))


def publicar_instagram(fila: dict) -> str:
    img = (fila.get("url_imagen") or "").strip()
    if not img:
        raise RuntimeError("Instagram exige imagen y la fila no trae url_imagen")
    cont = post(f"{IG_USER_ID}/media", {"image_url": img, "caption": texto(fila, "ig")})
    creation_id = cont["id"]

    # El contenedor tarda en quedar listo; Meta falla si se publica antes.
    for intento in range(12):
        time.sleep(5)
        estado = get(creation_id, {"fields": "status_code,status"})
        code = estado.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"Instagram no pudo procesar la imagen: {estado.get('status')}")
    else:
        raise RuntimeError("Instagram no terminó de procesar la imagen en 60 s")

    r = post(f"{IG_USER_ID}/media_publish", {"creation_id": creation_id})
    return str(r.get("id"))


def main() -> int:
    if not TOKEN:
        log("ERROR: falta el secreto META_TOKEN en el repositorio.")
        return 1

    hoy = dt.datetime.now(dt.timezone.utc).date().isoformat()

    if not os.path.exists(CALENDARIO):
        log(f"ERROR: no encuentro {CALENDARIO}.")
        return 1

    import csv
    filas = list(csv.DictReader(open(CALENDARIO, encoding="utf-8-sig")))
    fila = next((f for f in filas if (f.get("fecha") or "").strip() == hoy), None)

    if fila is None:
        ultima = filas[-1]["fecha"] if filas else "?"
        if hoy > ultima:
            log(f"El calendario se agotó el {ultima}. Hay que cargar el siguiente trimestre.")
            return 1
        log(f"No hay fila para {hoy}: día en blanco a propósito. Nada que publicar.")
        return 0

    bitacora = ""
    if os.path.exists(BITACORA):
        bitacora = open(BITACORA, encoding="utf-8").read()
    if any(l.startswith(hoy) and "PENDIENTE" not in l for l in bitacora.splitlines()):
        log(f"Ya hay constancia de publicación para {hoy}. No repito.")
        return 0

    log(f"Publicando día {fila.get('dia')} ({hoy}): {fila.get('tema')}")

    fb_id = ig_id = None
    errores = []

    try:
        fb_id = publicar_facebook(fila)
        log(f"  Facebook OK: {fb_id}")
    except Exception as e:  # noqa: BLE001
        errores.append(f"Facebook: {e}")
        log(f"  Facebook FALLÓ: {e}")

    try:
        ig_id = publicar_instagram(fila)
        log(f"  Instagram OK: {ig_id}")
    except Exception as e:  # noqa: BLE001
        errores.append(f"Instagram: {e}")
        log(f"  Instagram FALLÓ: {e}")

    if fb_id or ig_id:
        redes = " + ".join(n for n, v in (("Facebook", fb_id), ("Instagram", ig_id)) if v)
        ids = " / ".join(f"{n} {v}" for n, v in (("FB", fb_id), ("IG", ig_id)) if v)
    else:
        redes, ids = "PENDIENTE MANUAL", "-"

    linea = f"{hoy} | dia {fila.get('dia')} | {redes} | {ids} | {fila.get('tema')}\n"
    with open(BITACORA, "a", encoding="utf-8") as fh:
        fh.write(linea)
    log(f"Bitácora: {linea.strip()}")

    if errores:
        log("\nFALLAS:\n  " + "\n  ".join(errores))
        return 1 if not (fb_id or ig_id) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
