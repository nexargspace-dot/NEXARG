 from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import sqlite3
from datetime import datetime
import json
import os
import secrets

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app, resources={r"/api/*": {
    "origins": os.environ.get(
        'CORS_ORIGINS',
         'https://nexarg.space,https://www.nexarg.space,http://localhost:5000,http://127.0.0.1:5000,http://127.0.0.1:5500'
    ).split(',')
}})
DB = 'nmstudio.db'
ORDERS_DIR = 'pedidos'

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
    CREATE TABLE IF NOT EXISTS aurea_productos (
        id TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        precio INTEGER NOT NULL DEFAULT 0,
        stock INTEGER NOT NULL DEFAULT 0,
        imagen TEXT,
        descripcion TEXT
    );
    CREATE TABLE IF NOT EXISTS aurea_pedidos (
        id TEXT PRIMARY KEY,
        cliente_nombre TEXT NOT NULL,
        cliente_gmail TEXT,
        cliente_telefono TEXT,
        total INTEGER NOT NULL DEFAULT 0,
        estado TEXT NOT NULL,
        creado TEXT NOT NULL,
        historial TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS aurea_pedido_items (
        pedido_id TEXT NOT NULL,
        producto_id TEXT NOT NULL,
        nombre TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        precio_unitario INTEGER NOT NULL
    );
    """)
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(pedidos)").fetchall()}
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
    if conn.execute("SELECT COUNT(*) FROM aurea_productos").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO aurea_productos (id, nombre, precio, stock, imagen, descripcion) VALUES (?,?,?,?,?,?)",
            [
                ('prod-01', 'Champú Reparador', 9500, 10, 'assets/prod-01.png', 'Limpieza profunda y reparación capilar.'),
                ('prod-02', 'Sérum Nutritivo', 12800, 10, 'assets/prod-02.png', 'Producto exclusivo de nuestra línea boutique profesional.'),
                ('prod-03', 'Máscara Intensiva', 14200, 10, 'assets/prod-03.png', 'Nutrición intensa para cabellos secos o dañados.'),
                ('prod-04', 'Aceite Capilar', 11200, 10, 'assets/prod-04.jpg', 'Brillo y suavidad sin dejar sensación pesada.'),
                ('prod-05', 'Crema de Peinar', 8700, 10, 'assets/prod-05.png', 'Control de frizz y definición para todo tipo de cabello.'),
                ('prod-06', 'Kit Viaje Aurea', 16500, 10, 'assets/prod-06.png', 'Tus esenciales en tamaño ideal para llevar a todos lados.')
            ]
        )
    # Cargar las 4 plantillas reales
    count = conn.execute("SELECT COUNT(*) FROM plantillas").fetchone()[0]
    if count == 0:
        plantillas = [
            ('peluqueria-aurea','Aurea - Salón de Belleza','servicios','peluqueria',355000,'$355.000','turnos','./plantillas/peluqueria/01-peluqueria/assets/img-hero.png','Diseño exclusivo para peluquerías'),
            ('minimal-studio','Minimal Studio','inmobiliarias','alquileres',352000,'$352.000','inmobiliaria','https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?w=600&q=80&auto=format&fit=crop','Enfoque minimalista para galerías'),
            ('boutique-concept','Boutique Concept','comercios','cosmeticos',348000,'$348.000','mayorista','https://images.unsplash.com/photo-1441986300917-64674bd600d8?w=600&q=80&auto=format&fit=crop','Ideal para indumentaria y catálogo visual'),
            ('plantilla-4','Nueva Plantilla 4','gastronomia','resto',355000,'$355.000','ecommerce','./plantillas/nueva-4/assets/foto-principal.jpg','Descripción corta de la web'),
        ]
        conn.executemany("INSERT INTO plantillas VALUES (?,?,?,?,?,?,?,?,?)", plantillas)
        print("4 Plantillas cargadas!")
    conn.commit()
    conn.close()
    print("DB lista!")

init_db()

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'service': 'nexarg-api'})

@app.route('/admin')
def admin():
    return send_from_directory(app.static_folder, 'admin.html')

@app.route('/favicon-180x180.png')
def favicon_180():
    return send_from_directory('assets', 'Fondo Transparente (1).png')

@app.route('/favicon-512x512.png')
def favicon_512():
    return send_from_directory('assets', 'Fondo Transparente (1).png')

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

@app.route('/api/aurea/productos', methods=['GET', 'PUT'])
def aurea_productos():
    conn = get_db()
    if request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        incoming = data.get('productos') or []
        if not isinstance(incoming, list):
            conn.close()
            return jsonify({'error': 'El formato de productos es inválido.'}), 400
        try:
            conn.execute('BEGIN IMMEDIATE')
            for item in incoming:
                product_id = str(item.get('id') or '').strip()
                name = str(item.get('nombre') or '').strip()
                price = int(item.get('precio') or 0)
                stock = int(item.get('stock') or 0)
                if not product_id or not name or price < 0 or stock < 0:
                    raise ValueError('Producto, precio o stock inválido.')
                conn.execute(
                    "INSERT INTO aurea_productos (id, nombre, precio, stock, imagen, descripcion) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET nombre=excluded.nombre, precio=excluded.precio, stock=excluded.stock, "
                    "imagen=excluded.imagen, descripcion=excluded.descripcion",
                    (product_id, name, price, stock, str(item.get('imagen') or ''), str(item.get('descripcion') or ''))
                )
            conn.commit()
        except (ValueError, TypeError) as error:
            conn.rollback()
            conn.close()
            return jsonify({'error': str(error)}), 400
        conn.close()
        return jsonify({'ok': True})
    rows = conn.execute("SELECT * FROM aurea_productos ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route('/api/aurea/pedidos', methods=['GET', 'POST'])
def aurea_pedidos():
    conn = get_db()
    if request.method == 'GET':
        orders = []
        for row in conn.execute("SELECT * FROM aurea_pedidos ORDER BY creado DESC").fetchall():
            order = dict(row)
            order['productos'] = [
                dict(item) for item in conn.execute(
                    "SELECT producto_id AS id, nombre, cantidad AS qty, precio_unitario AS precio "
                    "FROM aurea_pedido_items WHERE pedido_id=?",
                    (row['id'],)
                ).fetchall()
            ]
            orders.append(order)
        conn.close()
        return jsonify(orders)

    data = request.get_json(silent=True) or {}
    client = data.get('cliente') or {}
    items = data.get('productos') or []
    name = str(client.get('nombre') or '').strip()
    if not name or not items:
        conn.close()
        return jsonify({'error': 'El nombre y al menos un producto son obligatorios.'}), 400

    try:
        conn.execute('BEGIN IMMEDIATE')
        normalized = []
        total = 0
        for item in items:
            product_id = str(item.get('id') or '').strip()
            quantity = int(item.get('qty') or 0)
            if not product_id or quantity <= 0:
                raise ValueError('Producto o cantidad inválida.')
            product = conn.execute("SELECT * FROM aurea_productos WHERE id=?", (product_id,)).fetchone()
            if not product:
                raise ValueError('Producto no encontrado.')
            if product['stock'] < quantity:
                conn.rollback()
                return jsonify({'error': f'Sin stock suficiente para {product["nombre"]}.'}), 409
            normalized.append((product, quantity))
            total += product['precio'] * quantity

        order_id = f"AR-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2).upper()}"
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO aurea_pedidos (id, cliente_nombre, cliente_gmail, cliente_telefono, total, estado, creado, historial) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (order_id, name, str(client.get('gmail') or '').strip(), str(client.get('telefono') or '').strip(),
             total, 'NUEVO', now, 'NUEVO')
        )
        for product, quantity in normalized:
            conn.execute(
                "INSERT INTO aurea_pedido_items (pedido_id, producto_id, nombre, cantidad, precio_unitario) VALUES (?,?,?,?,?)",
                (order_id, product['id'], product['nombre'], quantity, product['precio'])
            )
            conn.execute("UPDATE aurea_productos SET stock=stock-? WHERE id=?", (quantity, product['id']))
        conn.commit()
        conn.close()
        return jsonify({'id': order_id, 'total': total, 'estado': 'NUEVO'}), 201
    except (ValueError, TypeError) as error:
        conn.rollback()
        conn.close()
        return jsonify({'error': str(error)}), 400
    except Exception:
        conn.rollback()
        conn.close()
        raise

@app.route('/api/aurea/pedidos/<order_id>/estado', methods=['PUT'])
def aurea_cambiar_estado(order_id):
    data = request.get_json(silent=True) or {}
    new_status = str(data.get('estado') or '').strip().upper()
    allowed = {'NUEVO', 'CONFIRMADO', 'ENTREGADO', 'CANCELADO'}
    if new_status not in allowed:
        return jsonify({'error': 'Estado inválido.'}), 400
    conn = get_db()
    conn.execute('BEGIN IMMEDIATE')
    row = conn.execute("SELECT estado, historial FROM aurea_pedidos WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.rollback()
        conn.close()
        return jsonify({'error': 'Pedido no encontrado.'}), 404
    if row['estado'] == 'CANCELADO' and new_status != 'CANCELADO':
        conn.rollback()
        conn.close()
        return jsonify({'error': 'Un pedido cancelado no puede reabrirse.'}), 409
    if new_status == 'CANCELADO' and row['estado'] != 'CANCELADO':
        for item in conn.execute("SELECT producto_id, cantidad FROM aurea_pedido_items WHERE pedido_id=?", (order_id,)).fetchall():
            conn.execute("UPDATE aurea_productos SET stock=stock+? WHERE id=?", (item['cantidad'], item['producto_id']))
    history = f"{row['historial']} -> {new_status}"
    conn.execute("UPDATE aurea_pedidos SET estado=?, historial=? WHERE id=?", (new_status, history, order_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/pedidos', methods=['GET','POST'])
def pedidos():
    conn = get_db()
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        client = data.get('cliente') or {}
        customization = data.get('customization') or {}
        name = str(client.get('nombre') or client.get('name') or '').strip()
        email = str(client.get('email') or '').strip()
        whatsapp = str(client.get('whatsapp') or '').strip()
        if not name or not email or not whatsapp:
            conn.close()
            return jsonify({'error': 'Nombre, email y WhatsApp son obligatorios.'}), 400

        now = datetime.now()
        serial = f"NEX-{now.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
        pedido_id = serial
        template_id = str(data.get('template') or 'peluqueria-aurea')
        total = int(data.get('total') or 0)
        delivery_email = str(data.get('gmailEntrega') or client.get('gmail') or email).strip()
        publication_mode = str(data.get('modalidadPublicacion') or 'carpeta-descargable').strip()
        domain = str(data.get('dominio') or '').strip()
        notes = str(data.get('observaciones') or '').strip()
        customization_json = json.dumps(customization, ensure_ascii=False)
        columns = ('id, cliente_nombre, cliente_whatsapp, cliente_email, total, estado, fecha, historial, '
                   'plantilla_id, serie, personalizacion_json, gmail_entrega, modalidad_publicacion, dominio, '
                   'observaciones, estado_pago')
        values = (pedido_id, name, whatsapp, email, total, 'RECIBIDO', now.isoformat(), 'RECIBIDO',
                  template_id, serial, customization_json, delivery_email, publication_mode, domain, notes,
                  'PENDIENTE')
        conn.execute(f"INSERT INTO pedidos ({columns}) VALUES ({','.join(['?'] * len(values))})", values)
        for item in data.get('items', []):
            conn.execute("INSERT INTO pedido_items VALUES (?,?,?,?)",
                         (pedido_id, item.get('id'), item.get('nombre'), item.get('precio', 0)))
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
    else:
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

@app.route('/api/pedidos/<id>/estado', methods=['PUT'])
def cambiar_estado(id):
    nuevo = request.json['estado']
    conn = get_db()
    row = conn.execute("SELECT historial FROM pedidos WHERE id=?", (id,)).fetchone()
    hist = row['historial'] + f" -> {nuevo}" if row else nuevo
    conn.execute("UPDATE pedidos SET estado=?, historial=? WHERE id=?", (nuevo, hist, id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', host='0.0.0.0', port=port)
