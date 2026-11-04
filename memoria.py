"""
NEXARG AI · memoria.py
Memoria permanente de conversaciones en Supabase (proyecto NEXARG AI).

Usa la API REST de Supabase con `requests` (la misma librería que ya usa app.py),
así no hace falta instalar nada nuevo.

Variables de entorno en Render:
  SUPABASE_URL          -> https://csamiqwwfwgdrcognwpa.supabase.co
  SUPABASE_SERVICE_KEY  -> secret key (sb_secret_...) o la service_role legacy.
                           NUNCA en el frontend ni en GitHub.
"""

import os
from datetime import datetime, timezone

import requests

HISTORIAL_MAX = 20  # cuántos mensajes anteriores le pasamos a Claude


def _cfg():
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
    clave = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {"apikey": clave, "Content-Type": "application/json"}
    # La service_role legacy es un JWT (empieza con eyJ) y también va en Authorization.
    # Las claves nuevas (sb_secret_...) van solo en apikey.
    if clave.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {clave}"
    return url, headers


def _get(tabla, params):
    url, headers = _cfg()
    r = requests.get(f"{url}/{tabla}", headers=headers, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _post(tabla, fila, prefer="return=representation"):
    url, headers = _cfg()
    return requests.post(f"{url}/{tabla}", headers={**headers, "Prefer": prefer},
                         json=fila, timeout=10)


def obtener_negocio(phone_number_id):
    """Busca qué negocio es según el Phone Number ID que recibió el mensaje."""
    filas = _get("ai_negocios", {"phone_number_id": f"eq.{phone_number_id}",
                                 "activo": "eq.true", "limit": 1})
    return filas[0] if filas else None


def obtener_o_crear_contacto(negocio_id, wa_id, nombre=None):
    """Devuelve el contacto; si es la primera vez que escribe, lo crea."""
    filtro = {"negocio_id": f"eq.{negocio_id}", "wa_id": f"eq.{wa_id}", "limit": 1}
    filas = _get("ai_contactos", filtro)
    if filas:
        contacto = filas[0]
        cambios = {"ultimo_mensaje_en": datetime.now(timezone.utc).isoformat()}
        if nombre and nombre != contacto.get("nombre"):
            cambios["nombre"] = nombre
        url, headers = _cfg()
        requests.patch(f"{url}/ai_contactos", headers=headers,
                       params={"id": f"eq.{contacto['id']}"}, json=cambios, timeout=10)
        return contacto

    r = _post("ai_contactos", {"negocio_id": negocio_id, "wa_id": wa_id, "nombre": nombre})
    if r.status_code == 409:  # lo creó otro mensaje al mismo tiempo
        return _get("ai_contactos", filtro)[0]
    r.raise_for_status()
    return r.json()[0]


def guardar_mensaje(negocio_id, contacto_id, rol, contenido, tipo="texto", wa_message_id=None):
    """
    Guarda un mensaje. Devuelve False si ya existía (Meta a veces manda
    el mismo webhook dos veces): en ese caso NO hay que responder de nuevo.
    """
    r = _post("ai_mensajes", {
        "negocio_id": negocio_id,
        "contacto_id": contacto_id,
        "rol": rol,
        "contenido": contenido,
        "tipo": tipo,
        "wa_message_id": wa_message_id,
    }, prefer="return=minimal")
    if r.status_code == 409:
        return False
    r.raise_for_status()
    return True


def cargar_historial(contacto_id, limite=HISTORIAL_MAX):
    """
    Devuelve los últimos mensajes en el formato de la API de Claude:
    [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    """
    filas = _get("ai_mensajes", {"contacto_id": f"eq.{contacto_id}",
                                 "select": "rol,contenido",
                                 "order": "id.desc", "limit": limite})
    filas.reverse()

    mensajes = []
    for f in filas:
        # Si hay dos seguidos del mismo rol (ej. el cliente mandó 3 mensajes), se juntan
        if mensajes and mensajes[-1]["role"] == f["rol"]:
            mensajes[-1]["content"] += "\n" + f["contenido"]
        else:
            mensajes.append({"role": f["rol"], "content": f["contenido"]})

    # Claude necesita que la conversación arranque con el cliente
    while mensajes and mensajes[0]["role"] != "user":
        mensajes.pop(0)
    return mensajes


def borrar_historial(contacto_id):
    """Para el comando /reset: borra todos los mensajes de ese contacto."""
    url, headers = _cfg()
    r = requests.delete(f"{url}/ai_mensajes", headers=headers,
                        params={"contacto_id": f"eq.{contacto_id}"}, timeout=10)
    r.raise_for_status()