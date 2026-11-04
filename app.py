 from flask import Flask, jsonify, send_from_directory, abort, request
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime, timedelta
import json
import os
import secrets
import shutil
import sqlite3

import threading
import time
from collections import deque, OrderedDict

import requests  # NEXARG AI: para hablar con WhatsApp y con Claude

import seguridad
from seguridad import requiere_admin, limitar, texto, imagen_valida, hash_token

# Solo API: no se sirve ninguna carpeta como estática (el frontend vive en Netlify/Cloudflare).
app = Flask(__name__, static_folder=None)
# Render pone la app detrás de un proxy: así request.remote_addr es la IP real del cliente.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1 MB por request
CORS(app, resources={r"/api/*": {
    "origins": os.environ.get(
        'CORS_ORIGINS',
         'https://nexarg.space,https://www.nexarg.space,http://localhost:5000,http://127.0.0.1:5000,http://127.0.0.1:5500'
    ).split(','),
    "allow_headers": ["Content-Type", "Authorization", "Accept"],
}})
seguridad.registrar(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB = os.path.join(DATA_DIR, 'nmstudio.db')
ORDERS_DIR = os.path.join(DATA_DIR, 'pedidos')
ASSETS_DIR = os.path.join(BASE_DIR, 'assets')
IMAGENES_PERMITIDAS = {'.png', '.jpg', '.jpeg', '.webp', '.svg'}

os.makedirs(DATA_DIR, exist_ok=True)
# Migra la base vieja de la raíz del proyecto a data/ (una sola vez).
_db_vieja = os.path.join(BASE_DIR, 'nmstudio.db')
if os.path.exists(_db_vieja) and not os.path.exists(DB):
    shutil.move(_db_vieja, DB)

# ------------------------------------------------------------
# TIENDAS: cada plantilla con carrito tiene sus propias tablas
# (<slug>_productos, <slug>_pedidos, <slug>_pedido_items).
# Para sumar una plantilla nueva: agregarla acá y listo.
# Solo se aceptan los slugs de esta lista, así el nombre de tabla
# que se arma en las consultas nunca viene "crudo" del usuario.
# ------------------------------------------------------------
TIENDAS = {
    'aurea':  {'prefijo_pedido': 'AR', 'marca': 'Aurea'},
    'lumina': {'prefijo_pedido': 'LU', 'marca': 'Lumina'},
}

def tienda_o_404(slug):
    if slug not in TIENDAS:
        abort(404)
    return slug

def seed_productos(marca):
    return [
        ('prod-01', 'Champú Reparador', 9500, 10, 'assets/prod-01.png', 'Limpieza profunda y reparación capilar.'),
        ('prod-02', 'Sérum Nutritivo', 12800, 10, 'assets/prod-02.png', 'Producto exclusivo de nuestra línea boutique profesional.'),
        ('prod-03', 'Máscara Intensiva', 14200, 10, 'assets/prod-03.png', 'Nutrición intensa para cabellos secos o dañados.'),
        ('prod-04', 'Aceite Capilar', 11200, 10, 'assets/prod-04.jpg', 'Brillo y suavidad sin dejar sensación pesada.'),
        ('prod-05', 'Crema de Peinar', 8700, 10, 'assets/prod-05.png', 'Control de frizz y definición para todo tipo de cabello.'),
        ('prod-06', f'Kit Viaje {marca}', 16500, 10, 'assets/prod-06.png', 'Tus esenciales en tamaño ideal para llevar a todos lados.')
    ]

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS plantillas (
        id TEXT PRIMARY KEY,
        nombre TEXT,
        categoria TEXT,
        subcategoria TEXT,
        precio_num INTEGER,
        precio TEXT,
        sistema TEXT,
        thumb TEXT,
        desc_text TEXT
    );
    CREATE TABLE IF NOT EXISTS pedidos (
        id TEXT PRIMARY KEY,
        cliente_nombre TEXT,
        cliente_whatsapp TEXT,
        cliente_email TEXT,
        total INTEGER,
        estado TEXT,
        fecha TEXT,
        historial TEXT,
        plantilla_id TEXT,
        serie TEXT,
        personalizacion_json TEXT,
        gmail_entrega TEXT,
        modalidad_publicacion TEXT,
        dominio TEXT,
        observaciones TEXT,
        estado_pago TEXT
    );
    CREATE TABLE IF NOT EXISTS pedido_items (
        pedido_id TEXT,
        plantilla_id TEXT,
        nombre TEXT,
        precio INTEGER
    );
    """)

    # Tablas de cada tienda (aurea_*, lumina_*)
    for t in TIENDAS:
        conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {t}_productos (
            id TEXT PRIMARY KEY,
            nombre TEXT NOT NULL,
            precio INTEGER NOT NULL DEFAULT 0,
            stock INTEGER NOT NULL DEFAULT 0,
            imagen TEXT,
            descripcion TEXT
        );
        CREATE TABLE IF NOT EXISTS {t}_pedidos (
            id TEXT PRIMARY KEY,
            cliente_nombre TEXT NOT NULL,
            cliente_gmail TEXT,
            cliente_telefono TEXT,
            total INTEGER NOT NULL DEFAULT 0,
            estado TEXT NOT NULL,
            creado TEXT NOT NULL,
            historial TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {t}_pedido_items (
            pedido_id TEXT NOT NULL,
            producto_id TEXT NOT NULL,
            nombre TEXT NOT NULL,
            cantidad INTEGER NOT NULL,
            precio_unitario INTEGER NOT NULL
        );
        """)
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({t}_pedidos)").fetchall()}
        if 'cancel_token_hash' not in cols:
            conn.execute(f"ALTER TABLE {t}_pedidos ADD COLUMN cancel_token_hash TEXT")

    existing_columns ={row[1] for row in conn.execute("PRAGMA table_info(pedidos)").fetchall()}
    new_columns = {
        'plantilla_id': 'TEXT',
        'serie': 'TEXT',
        'personalizacion_json': 'TEXT',
        'gmail_entrega': 'TEXT',
        'modalidad_publicacion': 'TEXT',
        'dominio': 'TEXT',
        'observaciones': 'TEXT',
        'estado_pago': 'TEXT'
    }
    for name, column_type in new_columns.items():
        if name not in existing_columns:
            conn.execute(f"ALTER TABLE pedidos ADD COLUMN {name} {column_type}")

    # Productos de arranque de cada tienda
    for t, cfg in TIENDAS.items():
        if conn.execute(f"SELECT COUNT(*) FROM {t}_productos").fetchone()[0] == 0:
            conn.executemany(
                f"INSERT INTO {t}_productos (id, nombre, precio, stock, imagen, descripcion) VALUES (?,?,?,?,?,?)",
                seed_productos(cfg['marca'])
            )

    # Cargar las plantillas del catálogo (las que ya existen se saltean)
    plantillas = [
        ('peluqueria-aurea','Aurea - Salón de Belleza','servicios','peluqueria',310000,'$310.000','turnos','./plantillas/peluqueria/01-peluqueria/assets/img-hero.png','Diseño exclusivo para peluquerías'),
        ('peluqueria-lumina','Lumina - Salón de Belleza','servicios','peluqueria',312000,'$312.000','turnos','./plantillas/peluqueria/02-peluqueria/assets/03.jpg','Diseño oscuro y editorial para salones de belleza'),
        ('minimal-studio','Minimal Studio','inmobiliarias','alquileres',352000,'$352.000','inmobiliaria','https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?w=600&q=80&auto=format&fit=crop','Enfoque minimalista para galerías'),
        ('boutique-concept','Boutique Concept','comercios','cosmeticos',348000,'$348.000','mayorista','https://images.unsplash.com/photo-1441986300917-64674bd600d8?w=600&q=80&auto=format&fit=crop','Ideal para indumentaria y catálogo visual'),
        ('plantilla-4','Nueva Plantilla 4','gastronomia','resto',355000,'$355.000','ecommerce','./plantillas/nueva-4/assets/foto-principal.jpg','Descripción corta de la web'),
    ]
    conn.executemany("INSERT OR IGNORE INTO plantillas VALUES (?,?,?,?,?,?,?,?,?)", plantillas)
    conn.commit()
    conn.close()
    print("DB lista!")

init_db()

@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'service': 'nexarg-api'})

@app.route('/assets/<path:nombre>')
def servir_imagen(nombre):
    # Solo imágenes y solo desde assets/ (send_from_directory ya impide salir con "../").
    if os.path.splitext(nombre.lower())[1] not in IMAGENES_PERMITIDAS:
        abort(404)
    return send_from_directory(ASSETS_DIR, nombre)

@app.route('/favicon-180x180.png')
def favicon_180():
    return send_from_directory(ASSETS_DIR, 'Fondo Transparente (1).png')

@app.route('/favicon-512x512.png')
def favicon_512():
    return send_from_directory(ASSETS_DIR, 'Fondo Transparente (1).png')

@app.route('/site.webmanifest')
def site_manifest():
    return jsonify({
        "name": "NEX ARG",
        "short_name": "NEXARG",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b0b0f",
        "theme_color": "#0b0b0f",
        "icons": [{
            "src": "/assets/Fondo%20Transparente%20(1).png",
            "sizes": "512x512",
            "type": "image/png"
        }]
    })

@app.route('/api/plantillas')
def get_plantillas():
    conn = get_db()
    rows = conn.execute("SELECT * FROM plantillas").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ------------------------------------------------------------
# PRODUCTOS POR TIENDA:  /api/aurea/productos  /api/lumina/productos
# GET es público (lo usa la web). Modificar requiere sesión de admin.
# ------------------------------------------------------------
@app.route('/api/<tienda>/productos', methods=['GET'])
def tienda_productos(tienda):
    t = tienda_o_404(tienda)
    conn = get_db()
    rows = conn.execute(f"SELECT * FROM {t}_productos ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route('/api/<tienda>/productos', methods=['PUT', 'POST'])
@requiere_admin
def tienda_guardar_productos(tienda):
    t = tienda_o_404(tienda)
    tabla = f'{t}_productos'
    data = request.get_json(silent=True) or {}
    incoming = data.get('productos') or []
    if not isinstance(incoming, list) or len(incoming) > 500:
        return jsonify({'error': 'El formato de productos es inválido.'}), 400
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        for item in incoming:
            if not isinstance(item, dict):
                raise ValueError('Producto inválido.')
            product_id = texto(item.get('id'), 80)
            name = texto(item.get('nombre'), 120)
            price = int(item.get('precio') or 0)
            stock = int(item.get('stock') or 0)
            if not product_id or not name or not (0 <= price <= 100_000_000) or not (0 <= stock <= 1_000_000):
                raise ValueError('Producto, precio o stock inválido.')
            conn.execute(
                f"INSERT INTO {tabla} (id, nombre, precio, stock, imagen, descripcion) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET nombre=excluded.nombre, precio=excluded.precio, stock=excluded.stock, "
                "imagen=excluded.imagen, descripcion=excluded.descripcion",
                (product_id, name, price, stock, imagen_valida(item.get('imagen')), texto(item.get('descripcion'), 1000))
            )
        conn.commit()
    except (ValueError, TypeError) as error:
        conn.rollback()
        conn.close()
        return jsonify({'error': str(error)}), 400
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/<tienda>/productos/<product_id>', methods=['DELETE'])
@requiere_admin
def eliminar_producto_tienda(tienda, product_id):
    t = tienda_o_404(tienda)
    conn = get_db()
    cursor = conn.execute(f"DELETE FROM {t}_productos WHERE id=?", (product_id,))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        return jsonify({'error': 'Producto no encontrado.'}), 404
    return jsonify({'ok': True})

# ------------------------------------------------------------
# PEDIDOS POR TIENDA:  /api/aurea/pedidos  /api/lumina/pedidos
# Crear un pedido es público (lo hace el cliente). Verlos y cambiarles
# el estado es solo para el admin.
# ------------------------------------------------------------
@app.route('/api/<tienda>/pedidos', methods=['GET'])
@requiere_admin
def tienda_listar_pedidos(tienda):
    t = tienda_o_404(tienda)
    conn = get_db()
    orders = []
    for row in conn.execute(f"SELECT * FROM {t}_pedidos ORDER BY creado DESC").fetchall():
        order = dict(row)
        order.pop('cancel_token_hash', None)
        order['productos'] = [
            dict(item) for item in conn.execute(
                f"SELECT producto_id AS id, nombre, cantidad AS qty, precio_unitario AS precio "
                f"FROM {t}_pedido_items WHERE pedido_id=?",
                (row['id'],)
            ).fetchall()
        ]
        orders.append(order)
    conn.close()
    return jsonify(orders)

@app.route('/api/<tienda>/pedidos', methods=['POST'])
@limitar(10, 600)
def tienda_crear_pedido(tienda):
    t = tienda_o_404(tienda)
    t_productos = f'{t}_productos'
    prefijo = TIENDAS[t]['prefijo_pedido']
    data = request.get_json(silent=True) or {}
    client = data.get('cliente') or {}
    items = data.get('productos') or []
    if not isinstance(client, dict) or not isinstance(items, list):
        return jsonify({'error': 'Formato de pedido inválido.'}), 400
    name = texto(client.get('nombre'), 120)
    if not name or not items:
        return jsonify({'error': 'El nombre y al menos un producto son obligatorios.'}), 400
    if len(items) > 50:
        return jsonify({'error': 'Demasiados productos en un solo pedido.'}), 400

    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        # Agrupa por id: así no se puede esquivar el control de stock repitiendo el mismo producto.
        cantidades = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Producto inválido.')
            product_id = texto(item.get('id'), 80)
            quantity = int(item.get('qty') or 0)
            if not product_id or not (1 <= quantity <= 100):
                raise ValueError('Producto o cantidad inválida.')
            cantidades[product_id] = cantidades.get(product_id, 0) + quantity
        normalized = []
        total = 0
        for product_id, quantity in cantidades.items():
            product = conn.execute(f"SELECT * FROM {t_productos} WHERE id=?", (product_id,)).fetchone()
            if not product:
                raise ValueError('Producto no encontrado.')
            if product['stock'] < quantity:
                conn.rollback()
                conn.close()
                return jsonify({'error': f'Sin stock suficiente para {product["nombre"]}.'}), 409
            normalized.append((product, quantity))
            total += product['precio'] * quantity

        order_id = f"{prefijo}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4).upper()}"
        cancel_token = secrets.token_urlsafe(24)
        now = datetime.now().isoformat()
        conn.execute(
            f"INSERT INTO {t}_pedidos (id, cliente_nombre, cliente_gmail, cliente_telefono, total, estado, creado, historial, cancel_token_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (order_id, name, texto(client.get('gmail'), 160), texto(client.get('telefono'), 40),
             total, 'NUEVO', now, 'NUEVO', hash_token(cancel_token))
        )
        for product, quantity in normalized:
            conn.execute(
                f"INSERT INTO {t}_pedido_items (pedido_id, producto_id, nombre, cantidad, precio_unitario) VALUES (?,?,?,?,?)",
                (order_id, product['id'], product['nombre'], quantity, product['precio'])
            )
            conn.execute(f"UPDATE {t_productos} SET stock=stock-? WHERE id=?", (quantity, product['id']))
        conn.commit()
        conn.close()
        # cancelToken: solo lo recibe quien creó el pedido, para poder anularlo si falla el turno.
        return jsonify({'id': order_id, 'total': total, 'estado': 'NUEVO', 'cancelToken': cancel_token}), 201
    except (ValueError, TypeError) as error:
        conn.rollback()
        conn.close()
        return jsonify({'error': str(error)}), 400
    except Exception:
        conn.rollback()
        conn.close()
        raise

def _cambiar_estado_tienda(t, order_id, new_status, cancel_token=None):
    allowed = {'NUEVO', 'CONFIRMADO', 'ENTREGADO', 'CANCELADO'}
    if new_status not in allowed:
        return jsonify({'error': 'Estado inválido.'}), 400
    conn = get_db()
    conn.execute('BEGIN IMMEDIATE')
    row = conn.execute(f"SELECT estado, historial, creado, cancel_token_hash FROM {t}_pedidos WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.rollback()
        conn.close()
        return jsonify({'error': 'Pedido no encontrado.'}), 404
    if cancel_token is not None:
        # Cancelación hecha por el propio cliente: token correcto, pedido nuevo y de hace menos de 30 min.
        valido = (
            row['cancel_token_hash']
            and secrets.compare_digest(row['cancel_token_hash'], hash_token(cancel_token))
            and row['estado'] == 'NUEVO'
            and datetime.fromisoformat(row['creado']) > datetime.now() - timedelta(minutes=30)
        )
        if not valido:
            conn.rollback()
            conn.close()
            return jsonify({'error': 'No se puede cancelar este pedido. Contactá al negocio.'}), 403
    if row['estado'] == 'CANCELADO' and new_status != 'CANCELADO':
        conn.rollback()
        conn.close()
        return jsonify({'error': 'Un pedido cancelado no puede reabrirse.'}), 409
    if new_status == 'CANCELADO' and row['estado'] != 'CANCELADO':
        for item in conn.execute(f"SELECT producto_id, cantidad FROM {t}_pedido_items WHERE pedido_id=?", (order_id,)).fetchall():
            conn.execute(f"UPDATE {t}_productos SET stock=stock+? WHERE id=?", (item['cantidad'], item['producto_id']))
    history = f"{row['historial']} -> {new_status}"
    conn.execute(f"UPDATE {t}_pedidos SET estado=?, historial=? WHERE id=?", (new_status, history, order_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/<tienda>/pedidos/<order_id>/estado', methods=['PUT'])
@requiere_admin
def tienda_cambiar_estado(tienda, order_id):
    t = tienda_o_404(tienda)
    data = request.get_json(silent=True) or {}
    return _cambiar_estado_tienda(t, order_id, texto(data.get('estado'), 20).upper())

@app.route('/api/<tienda>/pedidos/<order_id>/cancelar', methods=['POST'])
@limitar(10, 600)
def tienda_cancelar_pedido_cliente(tienda, order_id):
    t = tienda_o_404(tienda)
    data = request.get_json(silent=True) or {}
    token = texto(data.get('cancelToken'), 100)
    if not token:
        return jsonify({'error': 'Falta el código de cancelación.'}), 400
    return _cambiar_estado_tienda(t, order_id, 'CANCELADO', cancel_token=token)

# ------------------------------------------------------------
# PEDIDOS DE WEBS NEX ARG
# ------------------------------------------------------------
@app.route('/api/pedidos', methods=['GET'])
@requiere_admin
def listar_pedidos():
    conn = get_db()
    rows = conn.execute("SELECT * FROM pedidos ORDER BY fecha DESC").fetchall()
    result = []
    for r in rows:
        items = conn.execute("SELECT * FROM pedido_items WHERE pedido_id=?", (r['id'],)).fetchall()
        d = dict(r)
        d['items'] = [dict(i) for i in items]
        if d.get('personalizacion_json'):
            try:
                d['customization'] = json.loads(d['personalizacion_json'])
            except json.JSONDecodeError:
                d['customization'] = {}
        result.append(d)
    conn.close()
    return jsonify(result)

@app.route('/api/pedidos', methods=['POST'])
@limitar(5, 600)
def crear_pedido():
    data = request.get_json(silent=True) or {}
    client = data.get('cliente') or {}
    customization = data.get('customization') or {}
    if not isinstance(client, dict) or not isinstance(customization, dict):
        return jsonify({'error': 'Formato de pedido inválido.'}), 400
    name = texto(client.get('nombre') or client.get('name'), 120)
    email = texto(client.get('email'), 160)
    whatsapp = texto(client.get('whatsapp'), 40)
    if not name or not email or not whatsapp or '@' not in email:
        return jsonify({'error': 'Nombre, email y WhatsApp son obligatorios.'}), 400

    conn = get_db()
    template_id = texto(data.get('template') or 'peluqueria-aurea', 80)
    plantilla = conn.execute("SELECT id, nombre, precio_num FROM plantillas WHERE id=?", (template_id,)).fetchone()
    if not plantilla:
        conn.close()
        return jsonify({'error': 'Plantilla inexistente.'}), 400
    # El precio sale SIEMPRE de la base, nunca de lo que manda el navegador.
    total = int(plantilla['precio_num'] or 0)

    now = datetime.now()
    serial = f"NEX-{now.strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"
    pedido_id = serial
    delivery_email = texto(data.get('gmailEntrega') or client.get('gmail') or email, 160)
    publication_mode = texto(data.get('modalidadPublicacion') or 'carpeta-descargable', 40)
    domain = texto(data.get('dominio'), 120)
    notes = texto(data.get('observaciones'), 2000)
    customization_json = json.dumps(customization, ensure_ascii=False)
    columns = ('id, cliente_nombre, cliente_whatsapp, cliente_email, total, estado, fecha, historial, '
               'plantilla_id, serie, personalizacion_json, gmail_entrega, modalidad_publicacion, dominio, '
               'observaciones, estado_pago')
    values = (pedido_id, name, whatsapp, email, total, 'RECIBIDO', now.isoformat(), 'RECIBIDO',
              template_id, serial, customization_json, delivery_email, publication_mode, domain, notes,
              'PENDIENTE')
    conn.execute(f"INSERT INTO pedidos ({columns}) VALUES ({','.join(['?'] * len(values))})", values)
    conn.execute("INSERT INTO pedido_items VALUES (?,?,?,?)",
                 (pedido_id, plantilla['id'], plantilla['nombre'], total))
    conn.commit()
    conn.close()
    os.makedirs(os.path.join(ORDERS_DIR, serial), exist_ok=True)
    order_copy = {
        'id': pedido_id,
        'serie': serial,
        'template': template_id,
        'client': {'name': name, 'email': email, 'whatsapp': whatsapp},
        'gmailEntrega': delivery_email,
        'modalidadPublicacion': publication_mode,
        'dominio': domain,
        'observaciones': notes,
        'customization': customization,
        'createdAt': now.isoformat(),
        'paymentStatus': 'pending'
    }
    with open(os.path.join(ORDERS_DIR, serial, 'pedido.json'), 'w', encoding='utf-8') as file:
        json.dump(order_copy, file, ensure_ascii=False, indent=2)
    return jsonify({"id": pedido_id, "serie": serial, "status": "ok", "paymentStatus": "pending"})

@app.route('/api/pedidos/<id>/estado', methods=['PUT'])
@requiere_admin
def cambiar_estado(id):
    data = request.get_json(silent=True) or {}
    nuevo = texto(data.get('estado'), 30).upper()
    if nuevo not in {'RECIBIDO', 'EN_PREPARACION', 'ENTREGADO', 'CANCELADO', 'PAGADO'}:
        return jsonify({'error': 'Estado inválido.'}), 400
    conn = get_db()
    row = conn.execute("SELECT historial FROM pedidos WHERE id=?", (id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Pedido no encontrado.'}), 404
    conn.execute("UPDATE pedidos SET estado=?, historial=? WHERE id=?", (nuevo, f"{row['historial']} -> {nuevo}", id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ------------------------------------------------------------
# NEXARG AI - FASE 3: EL CEREBRO
# WhatsApp -> este servidor -> Claude (con la instrucción maestra) -> WhatsApp
#
# Variables de entorno en Render (Environment):
#   WHATSAPP_TOKEN     -> token permanente de Meta
#   PHONE_NUMBER_ID    -> "Phone Number ID" del número
#   VERIFY_TOKEN       -> palabra secreta del webhook (nexarg-verifica-2026)
#   ANTHROPIC_API_KEY  -> clave de la API de Claude (console.anthropic.com)
#   DUENO_WHATSAPP     -> número del dueño para avisos, sin + ni espacios (ej: 541151323616)
#   AGENTE_NOMBRE      -> (opcional) nombre de la vendedora. Por defecto: Luz
#   CLAUDE_MODEL       -> (opcional) modelo. Por defecto: claude-haiku-5-5
# ------------------------------------------------------------
GRAPH_URL = "https://graph.facebook.com/v25.0"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Memoria de conversaciones (en RAM). OJO: se borra si Render reinicia.
# En la Fase 4 la pasamos a Supabase para que sea permanente.
HISTORIAL_MAX = 20                      # últimos mensajes que recuerda por cliente
_historiales = {}                       # numero -> deque de {"role","content"}
_pausados = {}                          # numero -> timestamp hasta cuándo la IA no responde
_vistos = OrderedDict()                 # ids de mensajes ya procesados (Meta a veces reenvía)
_candados = {}                          # numero -> Lock, para no contestar dos veces a la vez
_lock_global = threading.Lock()

PAUSA_DERIVACION_SEG = 2 * 60 * 60      # si deriva al dueño, la IA se calla 2 horas en ese chat


def normalizar_numero_ar(numero):
    # Meta entrega los celulares argentinos como 549XXXXXXXXXX, pero el número
    # de prueba los guarda como 54XXXXXXXXXX (sin el 9). Esto lo corrige.
    if numero.startswith("549") and len(numero) == 13:
        return "54" + numero[3:]
    return numero


def _headers_meta():
    return {"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}",
            "Content-Type": "application/json"}


def enviar_whatsapp(destino, mensaje):
    respuesta = requests.post(
        f"{GRAPH_URL}/{os.environ['PHONE_NUMBER_ID']}/messages",
        headers=_headers_meta(),
        json={"messaging_product": "whatsapp", "to": normalizar_numero_ar(destino),
              "type": "text", "text": {"body": mensaje}},
        timeout=15,
    )
    if respuesta.status_code >= 400:
        print("Error al enviar a WhatsApp:", respuesta.status_code, respuesta.text)
    return respuesta


def marcar_leido_y_escribiendo(message_id):
    # Tildes azules + "escribiendo..." mientras la IA piensa. Si falla, no pasa nada.
    try:
        requests.post(
            f"{GRAPH_URL}/{os.environ['PHONE_NUMBER_ID']}/messages",
            headers=_headers_meta(),
            json={"messaging_product": "whatsapp", "status": "read",
                  "message_id": message_id, "typing_indicator": {"type": "text"}},
            timeout=10,
        )
    except Exception as error:
        print("No se pudo marcar como leído:", error)


# ------------------------------------------------------------
# DATOS DEL NEGOCIO (capa 3). Por ahora: NEX ARG.
# Más adelante cada negocio carga esto desde su panel.
# ------------------------------------------------------------
def datos_del_negocio():
    try:
        conn = get_db()
        # Solo las webs terminadas. Cuando sumes una nueva, agregá su id acá.
        disponibles = ('peluqueria-aurea', 'peluqueria-lumina')
        filas = conn.execute(
            f"SELECT nombre, precio, desc_text FROM plantillas WHERE id IN ({','.join('?' * len(disponibles))})",
            disponibles).fetchall()
        conn.close()
        catalogo = "\n".join(f"- {f['nombre']}: {f['precio']} — {f['desc_text']}" for f in filas)
        catalogo += ("\n- Lira (peluquería): PRÓXIMAMENTE, todavía sin precio. Si preguntan, decí que sale muy pronto "
                     "y ofrecé avisarle cuando esté.\n"
                     "Por ahora solo hay webs para peluquerías y salones de belleza. Otros rubros vienen pronto: "
                     "si el cliente tiene otro rubro, contale que se están sumando y usá [DERIVAR: interesado en rubro X].")
    except Exception as error:
        print("No se pudo leer el catálogo:", error)
        catalogo = "(catálogo no disponible ahora)"
    return f"""NEGOCIO: NEX ARG — webs premium por rubro para negocios y PYMEs (Quilmes, Buenos Aires, Argentina).
Web: https://nexarg.space (ahí está el catálogo completo y se compra online).

CATÁLOGO (precios finales, no inventes otros):
{catalogo}

POLÍTICAS:
- Entrega: 48 horas hábiles después de confirmado el pago.
- El cliente completa un formulario con sus datos y fotos, y la web se arma tal cual lo envió.
- Cambios post-entrega: solo errores de carga (foto, texto o teléfono mal) avisados dentro de las 2 horas hábiles de enviado el formulario.
- Hosting: queda en la cuenta del propio cliente.
- Dominio propio: se vende aparte e incluye 1 año gratis.
- Cuotas: con la tarjeta o el banco del cliente (no financia NEX ARG).
- Mantenimiento mensual opcional para cambios chicos (fotos, precios, textos), no rediseños.
- Garantía: 100% ante errores reales del sitio, con arreglo gratis o devolución.
- Extra opcional: sistema de pedidos/turnos con panel para el dueño y pagos con Mercado Pago.

CÓMO SE COMPRA: entrando a https://nexarg.space, eligiendo la plantilla y tocando "Obtener esta web"."""


def instruccion_maestra():
    nombre = os.environ.get("AGENTE_NOMBRE", "Luz")
    return f"""Sos {nombre}, la vendedora de NEX ARG que atiende el WhatsApp del negocio. Tu trabajo es ayudar y vender, como la mejor vendedora del local.

CÓMO ESCRIBÍS (capa humana):
- Escribís como una persona real por WhatsApp: mensajes cortos, naturales, cálidos.
- Si la respuesta tiene más de una idea, separala en 2 o 3 mensajes cortos usando ||| entre cada uno. Nunca más de 3.
- Una sola pregunta por vez.
- Copiás el estilo del cliente: si escribe corto y sin emojis, vos igual; si usa emojis, podés usar uno o dos.
- Usás voseo rioplatense ("querés", "tenés") si el cliente escribe en español de Argentina. Si escribe en otro idioma o variante, respondé en ese idioma y con su forma de hablar.
- Nada de palabras de catálogo ("exclusivo", "calce premium", "de forma impecable", "estimado cliente"). Hablás como una persona, no como un folleto.
- Sin listas largas ni formato raro: es WhatsApp, no un mail.
- Si es el primer mensaje, saludás corto y preguntás en qué lo podés ayudar o qué tipo de negocio tiene.

CÓMO VENDÉS (capa de ventas):
- Primero entendés qué necesita: qué negocio tiene, qué quiere lograr.
- Recomendás la opción que mejor le sirve, con un beneficio concreto para su negocio, no una lista de características.
- Ofrecés el complemento (dominio propio, sistema de turnos/pedidos, mantenimiento) recién DESPUÉS de que muestre interés en algo. Como consejo, nunca como presión.
- Si duda por el precio, mostrás el valor (vende 24/7, se ve profesional, entrega en 48 hs) y mencionás las cuotas.
- Cuando está listo, le pasás el link para comprar y le decís el siguiente paso.

REGLAS DE ORO:
- NUNCA inventes precios, plazos, productos, descuentos ni datos que no estén en la información del negocio.
- Si no sabés algo, o el cliente pide algo fuera de lo normal (descuento especial, trabajo a medida, reclamo, problema con un pago, quiere hablar con una persona), respondé algo como "Dejame consultarlo y te confirmo en un ratito 😊" y al FINAL de tu respuesta agregá la etiqueta [DERIVAR: motivo corto]. Esa etiqueta el cliente no la ve.
- Si te preguntan si sos un bot o una IA, decí la verdad con calidez: sos la asistente virtual de NEX ARG y lo ayudás con todo; si quiere, lo pasás con una persona (y ahí usás [DERIVAR: pide hablar con una persona]).
- No hables de temas que no tengan que ver con el negocio; volvé amablemente a lo que el cliente necesita.

INFORMACIÓN DEL NEGOCIO:
{datos_del_negocio()}"""


def preguntar_a_claude(historial):
    respuesta = requests.post(
        ANTHROPIC_URL,
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"),
              "max_tokens": 2000,
              "system": instruccion_maestra(),
              "messages": list(historial)},
        timeout=60,
    )
    if respuesta.status_code >= 400:
        print("Error de Claude:", respuesta.status_code, respuesta.text)
        return None
    datos = respuesta.json()
    # Solo nos quedamos con el texto (si el modelo "piensa", esos bloques se ignoran).
    return "".join(b.get("text", "") for b in datos.get("content", []) if b.get("type") == "text").strip()


def avisar_al_dueno(cliente, motivo, ultimo_mensaje):
    dueno = os.environ.get("DUENO_WHATSAPP")
    if not dueno:
        print("DERIVACIÓN sin DUENO_WHATSAPP configurado:", cliente, motivo)
        return
    enviar_whatsapp(dueno, f"🔔 NEXARG AI necesita que atiendas este chat\n"
                           f"Cliente: +{cliente}\nMotivo: {motivo}\n"
                           f"Último mensaje: \"{ultimo_mensaje[:300]}\"\n\n"
                           f"La IA queda en pausa 2 horas en ese chat.")


def responder_con_ia(remitente, recibido, message_id):
    with _lock_global:
        candado = _candados.setdefault(remitente, threading.Lock())
    with candado:
        try:
            # Comando de prueba: "/reset" borra la memoria y saca la pausa.
            if recibido.strip().lower() == "/reset":
                _historiales.pop(remitente, None)
                _pausados.pop(remitente, None)
                enviar_whatsapp(remitente, "🔄 Memoria borrada. Empecemos de nuevo.")
                return

            if _pausados.get(remitente, 0) > time.time():
                print(f"Chat {remitente} en pausa (lo atiende el dueño).")
                return

            marcar_leido_y_escribiendo(message_id)
            historial = _historiales.setdefault(remitente, deque(maxlen=HISTORIAL_MAX))
            historial.append({"role": "user", "content": recibido[:2000]})
            # La conversación tiene que empezar con un mensaje del cliente.
            while historial and historial[0]["role"] != "user":
                historial.popleft()

            texto_ia = preguntar_a_claude(historial)
            if not texto_ia:
                historial.pop()
                enviar_whatsapp(remitente, "Perdón, se me cortó algo 😅 ¿Me lo repetís en un ratito?")
                return

            # ¿Pidió derivar al dueño?
            motivo = None
            if "[DERIVAR" in texto_ia:
                inicio = texto_ia.index("[DERIVAR")
                fin = texto_ia.find("]", inicio)
                etiqueta = texto_ia[inicio:fin + 1] if fin != -1 else texto_ia[inicio:]
                motivo = etiqueta.strip("[]").replace("DERIVAR", "").strip(" :") or "sin detalle"
                texto_ia = texto_ia.replace(etiqueta, "").strip()

            historial.append({"role": "assistant", "content": texto_ia})

            # Toque humano: varios mensajes cortos, con una pausa según el largo.
            partes = [p.strip() for p in texto_ia.split("|||") if p.strip()][:3]
            for i, parte in enumerate(partes):
                if i > 0:
                    marcar_leido_y_escribiendo(message_id)
                    time.sleep(min(1.5 + len(parte) / 40, 6))
                enviar_whatsapp(remitente, parte)

            if motivo:
                _pausados[remitente] = time.time() + PAUSA_DERIVACION_SEG
                avisar_al_dueno(remitente, motivo, recibido)
        except Exception as error:
            print("Error en responder_con_ia:", error)


@app.route('/webhook', methods=['GET'])
def verificar_webhook():
    # Meta llama a esta ruta UNA vez para comprobar que el servidor es tuyo.
    modo = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    desafio = request.args.get("hub.challenge")
    if modo == "subscribe" and token and token == os.environ.get("VERIFY_TOKEN"):
        return desafio, 200
    return "Token de verificación incorrecto", 403


@app.route('/webhook', methods=['POST'])
def recibir_whatsapp():
    # Meta llama a esta ruta cada vez que alguien le escribe al número.
    # Respondemos 200 al toque y la IA trabaja en segundo plano (si no, Meta reintenta).
    datos = request.get_json(silent=True) or {}
    try:
        for entrada in datos.get("entry", []):
            for cambio in entrada.get("changes", []):
                valor = cambio.get("value", {})
                for mensaje in valor.get("messages", []):
                    mid = mensaje.get("id")
                    with _lock_global:
                        if mid in _vistos:
                            continue
                        _vistos[mid] = True
                        while len(_vistos) > 1000:
                            _vistos.popitem(last=False)
                    remitente = mensaje.get("from")
                    if mensaje.get("type") == "text":
                        recibido = mensaje["text"]["body"]
                        print(f"Mensaje de {remitente}: {recibido}")
                        threading.Thread(target=responder_con_ia,
                                         args=(remitente, recibido, mid), daemon=True).start()
                    else:
                        enviar_whatsapp(remitente, "Por ahora leo solo mensajes de texto 😊 ¿Me lo escribís?")
    except Exception as error:
        # Nunca devolvemos error a Meta: si no, reintenta el mismo mensaje muchas veces.
        print("Error procesando el webhook:", error)
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', host='0.0.0.0', port=port)