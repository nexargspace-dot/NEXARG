from flask import Flask, jsonify, send_from_directory, abort, request
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime, timedelta
import json
import os
import secrets
import shutil
import sqlite3

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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', host='0.0.0.0', port=port)
