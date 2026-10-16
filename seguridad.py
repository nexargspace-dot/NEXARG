"""Seguridad del backend NEX ARG.

- Login de admin: los paneles inician sesión con Supabase Auth y mandan el token en
  "Authorization: Bearer ...". Acá se le pregunta a Supabase de quién es ese token y
  solo pasan los emails de ADMIN_EMAILS (o <SLUG>_ADMIN_EMAILS para una tienda).
- Límite de intentos por IP para las rutas públicas que escriben.
- Bloqueo de archivos privados (segunda capa: la app ya no sirve archivos estáticos).
- Cabeceras de seguridad.

Variables de entorno:
  SUPABASE_URL        URL del proyecto Supabase
  SUPABASE_ANON_KEY   clave publishable/anon del proyecto (NUNCA la service_role)
  ADMIN_EMAILS        emails admin separados por coma (vacío = nadie es admin)
  <SLUG>_ADMIN_EMAILS admins de una tienda puntual, ej. AUREA_ADMIN_EMAILS
  NM_ADMIN_AUTH       "1" (por defecto) exige login. "0" lo apaga SOLO para pruebas locales.
"""
from collections import defaultdict, deque
from functools import wraps
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request

from flask import abort, jsonify, request

SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://ribzyjxwvmvbavezmubr.supabase.co').rstrip('/')
SUPABASE_ANON_KEY = os.environ.get('SUPABASE_ANON_KEY', '')
ADMIN_AUTH_ACTIVO = os.environ.get('NM_ADMIN_AUTH', '1') != '0'

if not ADMIN_AUTH_ACTIVO:
    print('ATENCIÓN: NM_ADMIN_AUTH=0, las rutas de admin quedan SIN login. No usar en producción.')
elif not SUPABASE_ANON_KEY:
    print('ATENCIÓN: falta SUPABASE_ANON_KEY; ningún admin va a poder iniciar sesión.')

def _emails_env(name):
    return {e.strip().lower() for e in os.environ.get(name, '').split(',') if e.strip()}

_token_cache = {}
_token_lock = threading.Lock()

def email_del_token(token):
    """Devuelve el email verificado del usuario de Supabase, o None si el token no sirve."""
    if not SUPABASE_ANON_KEY:
        return None
    now = time.time()
    with _token_lock:
        cached = _token_cache.get(token)
        if cached and cached[1] > now:
            return cached[0]
    req = urllib.request.Request(
        f'{SUPABASE_URL}/auth/v1/user',
        headers={'Authorization': f'Bearer {token}', 'apikey': SUPABASE_ANON_KEY}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            user = json.load(resp)
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None
    email = str(user.get('email') or '').lower() or None
    with _token_lock:
        if len(_token_cache) > 500:
            _token_cache.clear()
        _token_cache[token] = (email, now + 60)
    return email

def requiere_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not ADMIN_AUTH_ACTIVO:
            return view(*args, **kwargs)
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or len(auth) > 4096:
            return jsonify({'error': 'Tenés que iniciar sesión como administrador.'}), 401
        email = email_del_token(auth[7:].strip())
        if not email:
            return jsonify({'error': 'Sesión inválida o vencida. Volvé a iniciar sesión.'}), 401
        permitidos = _emails_env('ADMIN_EMAILS')
        tienda = kwargs.get('tienda')
        if tienda:
            permitidos |= _emails_env(f'{tienda.upper()}_ADMIN_EMAILS')
        if email not in permitidos:
            return jsonify({'error': 'Tu usuario no tiene permisos de administrador.'}), 403
        return view(*args, **kwargs)
    return wrapper

# Límite simple de pedidos por IP (en memoria; alcanza con un solo worker de gunicorn).
_hits = defaultdict(deque)
_hits_lock = threading.Lock()

def limitar(max_req, por_segundos):
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                key = (view.__name__, request.remote_addr)
                now = time.time()
                with _hits_lock:
                    q = _hits[key]
                    while q and q[0] < now - por_segundos:
                        q.popleft()
                    if len(q) >= max_req:
                        return jsonify({'error': 'Demasiados intentos. Probá de nuevo en unos minutos.'}), 429
                    q.append(now)
            return view(*args, **kwargs)
        return wrapper
    return deco

def texto(value, max_len):
    """Texto limpio y con largo máximo (evita que llenen la base con basura)."""
    return str(value or '').strip()[:max_len]

def imagen_valida(url):
    url = texto(url, 1000)
    if not url or url.startswith('https://') or url.startswith('idb://'):
        return url
    if ':' not in url and not url.startswith('//'):  # ruta relativa tipo assets/foto.jpg
        return url
    raise ValueError('La imagen debe ser una URL https o una ruta del sitio.')

def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

# Archivos que NUNCA deben poder descargarse.
_CARPETAS_PRIVADAS = {'data', 'pedidos', '__pycache__', 'node_modules'}
_EXTENSIONES_PRIVADAS = {'.py', '.pyc', '.db', '.sqlite', '.sqlite3', '.env', '.txt', '.json', '.log',
                         '.git', '.md', '.sql', '.yaml', '.yml', '.zip', '.bak', '.sh', '.toml', '.cfg', '.ini'}

def bloquear_archivos_privados():
    if request.path.startswith('/api/'):
        return None
    partes = [p for p in request.path.split('/') if p]
    for parte in partes:
        if parte.startswith('.') or parte.lower() in _CARPETAS_PRIVADAS:
            abort(404)
    if partes and os.path.splitext(partes[-1].lower())[1] in _EXTENSIONES_PRIVADAS:
        abort(404)
    return None

def cabeceras_seguridad(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    if request.is_secure:
        resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    if request.path.startswith('/api/'):
        resp.headers.setdefault('Cache-Control', 'no-store')
    return resp

def registrar(app):
    """Conecta los filtros de seguridad a la app Flask."""
    app.before_request(bloquear_archivos_privados)
    app.after_request(cabeceras_seguridad)

    @app.errorhandler(413)
    def demasiado_grande(_):
        return jsonify({'error': 'Los datos enviados son demasiado grandes.'}), 413
