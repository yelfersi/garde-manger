from contextlib import contextmanager
from datetime import datetime
import os
import re

from authlib.integrations.flask_client import OAuth
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from google import genai
import psycopg2
import psycopg2.extras

app = Flask(__name__)
# Clé secrète pour sécuriser les sessions utilisateur
app.secret_key = os.environ['SECRET_KEY']

# Configuration OAuth Google
GOOGLE_CLIENT_ID = os.environ['GOOGLE_CLIENT_ID']
GOOGLE_CLIENT_SECRET = os.environ['GOOGLE_CLIENT_SECRET']

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

# URL de connexion PostgreSQL (Supabase)
DATABASE_URL = os.environ['DATABASE_URL']

# Client Gemini (tri IA de la liste de courses)
client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])

ITEM_TYPES_SHOPPING = ('ingredient', 'household')
ITEM_TYPES_STOCK = ('ingredient', 'household', 'recipe')


# --- OUTILS ---
def get_db_connection():
  return psycopg2.connect(
      DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
  )


@contextmanager
def db():
  """Ouvre une connexion, fournit un curseur, commit à la fin (rollback si erreur)."""
  conn = get_db_connection()
  try:
    cursor = conn.cursor()
    yield cursor
    conn.commit()
  except Exception:
    conn.rollback()
    raise
  finally:
    conn.close()


def to_int_or_none(value):
  try:
    return int(value) if value not in (None, '') else None
  except (TypeError, ValueError):
    return None


def clean_item_type(value, allowed):
  return value if value in allowed else 'ingredient'


def clean_ingredients(raw_list):
  """Nettoie une liste d'ingrédients : espaces, vides, doublons (insensible à la casse)."""
  seen = set()
  result = []
  for raw in raw_list or []:
    name = str(raw).strip()
    if name and name.lower() not in seen:
      seen.add(name.lower())
      result.append(name)
  return result


def init_db():
  with db() as cursor:
    cursor.execute("""
      CREATE TABLE IF NOT EXISTS shopping_list (
          id SERIAL PRIMARY KEY,
          name TEXT NOT NULL,
          quantity TEXT,
          estimated_price REAL DEFAULT 0.0,
          item_type TEXT DEFAULT 'ingredient',
          bought INTEGER DEFAULT 0
      )
    """)
    cursor.execute("""
      CREATE TABLE IF NOT EXISTS inventory (
          id SERIAL PRIMARY KEY,
          name TEXT NOT NULL,
          quantity TEXT,
          location TEXT NOT NULL,
          expiry_date TEXT,
          item_type TEXT DEFAULT 'ingredient',
          recipe_id BIGINT NULL
      )
    """)
    cursor.execute("""
      CREATE TABLE IF NOT EXISTS expenses (
          id SERIAL PRIMARY KEY,
          description TEXT NOT NULL,
          amount REAL NOT NULL,
          date TEXT NOT NULL
      )
    """)
    cursor.execute("""
      CREATE TABLE IF NOT EXISTS recipes (
          id BIGSERIAL PRIMARY KEY,
          name TEXT NOT NULL,
          created_at TIMESTAMPTZ DEFAULT now()
      )
    """)
    cursor.execute("""
      CREATE TABLE IF NOT EXISTS recipe_ingredients (
          id BIGSERIAL PRIMARY KEY,
          recipe_id BIGINT NOT NULL,
          ingredient_name TEXT NOT NULL
      )
    """)
    # V2 : lien entre un article acheté et la ligne créée en stock
    cursor.execute(
        'ALTER TABLE shopping_list ADD COLUMN IF NOT EXISTS inventory_id BIGINT NULL'
    )


# --- GESTION DE LA SÉCURITÉ ---
@app.before_request
def require_login():
  allowed_routes = ['login', 'auth_google', 'auth_callback', 'static']
  if request.endpoint not in allowed_routes and 'user' not in session:
    return redirect(url_for('login'))


@app.route('/login')
def login():
  return render_template('login.html')


@app.route('/auth/google')
def auth_google():
  redirect_uri = url_for('auth_callback', _external=True)
  return google.authorize_redirect(redirect_uri)


@app.route('/auth/callback')
def auth_callback():
  token = google.authorize_access_token()
  user_info = token.get('userinfo')
  email = user_info.get('email')

  utilisateurs_autorises = [
      'yelfersi@gmail.com',
      'e.0548041564@gmail.com',
      'edenrelfersi@gmail.com',
  ]

  if email not in utilisateurs_autorises:
    return (
        f"Accès refusé ({email}) : tu n'as pas l'autorisation d'accéder à ce"
        ' garde-manger.',
        403,
    )

  session['user'] = user_info
  return redirect(url_for('index'))


@app.route('/logout')
def logout():
  session.pop('user', None)
  return redirect(url_for('login'))


# --- ROUTES PRINCIPALES ---
@app.route('/')
def index():
  return render_template('index.html', user=session.get('user'))


# --- API : LISTE DE COURSES ---
@app.route('/api/shopping', methods=['GET', 'POST'])
def handle_shopping():
  if request.method == 'POST':
    data = request.json or {}
    with db() as cursor:
      cursor.execute(
          """
          INSERT INTO shopping_list (name, quantity, estimated_price, item_type)
          VALUES (%s, %s, %s, %s)
          """,
          (
              data.get('name', '').strip(),
              data.get('quantity', ''),
              float(data.get('estimated_price') or 0),
              clean_item_type(data.get('item_type'), ITEM_TYPES_SHOPPING),
          ),
      )
    return jsonify({'status': 'success'})

  with db() as cursor:
    cursor.execute('SELECT * FROM shopping_list ORDER BY bought ASC, id DESC')
    rows = cursor.fetchall()
  return jsonify([dict(row) for row in rows])


@app.route('/api/shopping/<int:item_id>', methods=['PUT', 'DELETE'])
def update_shopping(item_id):
  with db() as cursor:
    if request.method == 'PUT':
      # Modification du contenu uniquement (l'état "acheté" passe par /buy et /unbuy)
      data = request.json or {}
      cursor.execute(
          """
          UPDATE shopping_list
          SET name = %s, quantity = %s, estimated_price = %s, item_type = %s
          WHERE id = %s
          """,
          (
              data.get('name'),
              data.get('quantity'),
              float(data.get('estimated_price') or 0),
              clean_item_type(data.get('item_type'), ITEM_TYPES_SHOPPING),
              item_id,
          ),
      )
    else:
      cursor.execute('DELETE FROM shopping_list WHERE id = %s', (item_id,))
  return jsonify({'status': 'success'})


@app.route('/api/shopping/<int:item_id>/buy', methods=['POST'])
def buy_shopping(item_id):
  """Coche un article : il est marqué acheté et ajouté automatiquement au stock."""
  data = request.json or {}
  location = data.get('location') or 'Placard'
  expiry_date = data.get('expiry_date') or ''

  with db() as cursor:
    cursor.execute('SELECT * FROM shopping_list WHERE id = %s', (item_id,))
    item = cursor.fetchone()
    if not item:
      return jsonify({'status': 'error', 'message': 'Article introuvable'}), 404

    # Évite un doublon en stock si l'article est déjà acheté et lié
    if item['bought'] and item.get('inventory_id'):
      return jsonify({'status': 'success', 'already': True})

    cursor.execute(
        """
        INSERT INTO inventory (name, quantity, location, expiry_date, item_type, recipe_id)
        VALUES (%s, %s, %s, %s, %s, NULL)
        RETURNING id
        """,
        (
            item['name'],
            item['quantity'] or '',
            location,
            expiry_date,
            clean_item_type(item['item_type'], ITEM_TYPES_STOCK),
        ),
    )
    inventory_id = cursor.fetchone()['id']
    cursor.execute(
        'UPDATE shopping_list SET bought = 1, inventory_id = %s WHERE id = %s',
        (inventory_id, item_id),
    )
  return jsonify({'status': 'success'})


@app.route('/api/shopping/<int:item_id>/unbuy', methods=['POST'])
def unbuy_shopping(item_id):
  """Décoche un article : annule l'achat et retire la ligne de stock créée."""
  with db() as cursor:
    cursor.execute('SELECT * FROM shopping_list WHERE id = %s', (item_id,))
    item = cursor.fetchone()
    if not item:
      return jsonify({'status': 'error', 'message': 'Article introuvable'}), 404
    if item.get('inventory_id'):
      cursor.execute('DELETE FROM inventory WHERE id = %s', (item['inventory_id'],))
    cursor.execute(
        'UPDATE shopping_list SET bought = 0, inventory_id = NULL WHERE id = %s',
        (item_id,),
    )
  return jsonify({'status': 'success'})


@app.route('/api/shopping/clear-bought', methods=['DELETE'])
def clear_bought_shopping():
  """Supprime de la liste tous les articles déjà achetés (le stock n'est pas touché)."""
  with db() as cursor:
    cursor.execute('DELETE FROM shopping_list WHERE bought = 1')
    deleted = cursor.rowcount
  return jsonify({'status': 'success', 'deleted': deleted})


# --- API : RECETTES ---
RECIPES_WITH_INGREDIENTS_SQL = """
    SELECT
        r.id,
        r.name,
        COALESCE(
            array_agg(ri.ingredient_name ORDER BY ri.id)
                FILTER (WHERE ri.id IS NOT NULL),
            ARRAY[]::text[]
        ) AS ingredients
    FROM recipes r
    LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
    GROUP BY r.id, r.name
    ORDER BY lower(r.name)
"""


def parse_recipes_text(text):
  """Transforme un texte en liste de recettes.

  Formats acceptés (recettes séparées par une ligne vide) :
      Gratin dauphinois          Soupe : carotte, poireau, oignon
      - pommes de terre
      - crème
  """
  recipes = []
  blocks = re.split(r'\n\s*\n', text.replace('\r\n', '\n').replace('\r', '\n').strip())
  bullet = re.compile(r'^\s*(?:[-*\u2022\u2013]|\d+[.)])\s*')

  for block in blocks:
    lines = [line.strip() for line in block.split('\n') if line.strip()]
    if not lines:
      continue

    first = lines[0].lstrip('#*').strip()
    ingredients = []
    if ':' in first:
      name, rest = first.split(':', 1)
      ingredients += [part for part in re.split(r'[;,]', rest) if part.strip()]
    else:
      name = first
    name = name.strip()
    if not name:
      continue

    for line in lines[1:]:
      ingredients.append(bullet.sub('', line))

    recipes.append({'name': name, 'ingredients': clean_ingredients(ingredients)})
  return recipes


@app.route('/api/recipes', methods=['GET'])
def get_recipes():
  with db() as cursor:
    cursor.execute(RECIPES_WITH_INGREDIENTS_SQL)
    rows = cursor.fetchall()
  return jsonify([dict(row) for row in rows])


@app.route('/api/recipes', methods=['POST'])
def create_recipe():
  data = request.json or {}
  name = (data.get('name') or '').strip()
  if not name:
    return jsonify({'status': 'error', 'message': 'Le nom est obligatoire'}), 400

  with db() as cursor:
    cursor.execute(
        'INSERT INTO recipes (name) VALUES (%s) RETURNING id', (name,)
    )
    recipe_id = cursor.fetchone()['id']
    for ingredient in clean_ingredients(data.get('ingredients')):
      cursor.execute(
          'INSERT INTO recipe_ingredients (recipe_id, ingredient_name) VALUES (%s, %s)',
          (recipe_id, ingredient),
      )
  return jsonify({'status': 'success', 'id': recipe_id})


@app.route('/api/recipes/<int:recipe_id>', methods=['PUT', 'DELETE'])
def update_recipe(recipe_id):
  with db() as cursor:
    if request.method == 'PUT':
      data = request.json or {}
      name = (data.get('name') or '').strip()
      if not name:
        return jsonify({'status': 'error', 'message': 'Le nom est obligatoire'}), 400
      cursor.execute('UPDATE recipes SET name = %s WHERE id = %s', (name, recipe_id))
      # La liste d'ingrédients est remplacée par celle reçue
      cursor.execute('DELETE FROM recipe_ingredients WHERE recipe_id = %s', (recipe_id,))
      for ingredient in clean_ingredients(data.get('ingredients')):
        cursor.execute(
            'INSERT INTO recipe_ingredients (recipe_id, ingredient_name) VALUES (%s, %s)',
            (recipe_id, ingredient),
        )
    else:
      cursor.execute('DELETE FROM recipe_ingredients WHERE recipe_id = %s', (recipe_id,))
      # Les plats en stock restent, mais ne sont plus liés à une recette
      cursor.execute('UPDATE inventory SET recipe_id = NULL WHERE recipe_id = %s', (recipe_id,))
      cursor.execute('DELETE FROM recipes WHERE id = %s', (recipe_id,))
  return jsonify({'status': 'success'})


@app.route('/api/recipes/<int:recipe_id>/ingredients')
def get_recipe_ingredients(recipe_id):
  with db() as cursor:
    cursor.execute(
        """
        SELECT ingredient_name
        FROM recipe_ingredients
        WHERE recipe_id = %s
        ORDER BY id
        """,
        (recipe_id,),
    )
    rows = cursor.fetchall()
  return jsonify([row['ingredient_name'] for row in rows])


@app.route('/api/recipes/import', methods=['POST'])
def import_recipes():
  data = request.json or {}
  parsed = parse_recipes_text(data.get('text') or '')
  if not parsed:
    return jsonify({'status': 'error', 'message': 'Aucune recette détectée'}), 400

  imported, skipped = [], []
  with db() as cursor:
    cursor.execute('SELECT lower(name) AS n FROM recipes')
    existing = {row['n'] for row in cursor.fetchall()}

    for recipe in parsed:
      key = recipe['name'].lower()
      if key in existing:
        skipped.append(recipe['name'])
        continue
      existing.add(key)
      cursor.execute(
          'INSERT INTO recipes (name) VALUES (%s) RETURNING id', (recipe['name'],)
      )
      recipe_id = cursor.fetchone()['id']
      for ingredient in recipe['ingredients']:
        cursor.execute(
            'INSERT INTO recipe_ingredients (recipe_id, ingredient_name) VALUES (%s, %s)',
            (recipe_id, ingredient),
        )
      imported.append(recipe['name'])

  return jsonify({'status': 'success', 'imported': imported, 'skipped': skipped})


# --- API : INVENTAIRE ---
@app.route('/api/inventory', methods=['GET', 'POST'])
def handle_inventory():
  if request.method == 'POST':
    data = request.json or {}
    item_type = clean_item_type(data.get('item_type'), ITEM_TYPES_STOCK)
    # Seul un plat préparé peut être lié à une recette
    recipe_id = to_int_or_none(data.get('recipe_id')) if item_type == 'recipe' else None
    with db() as cursor:
      cursor.execute(
          """
          INSERT INTO inventory (name, quantity, location, expiry_date, item_type, recipe_id)
          VALUES (%s, %s, %s, %s, %s, %s)
          """,
          (
              data.get('name', '').strip(),
              data.get('quantity', ''),
              data.get('location', 'Frigo'),
              data.get('expiry_date', ''),
              item_type,
              recipe_id,
          ),
      )
    return jsonify({'status': 'success'})

  with db() as cursor:
    cursor.execute("""
        SELECT i.*, r.name AS recipe_name
        FROM inventory i
        LEFT JOIN recipes r ON r.id = i.recipe_id
        ORDER BY i.location, i.id DESC
    """)
    rows = cursor.fetchall()
  return jsonify([dict(row) for row in rows])


@app.route('/api/inventory/<int:item_id>', methods=['PUT', 'DELETE'])
def update_inventory(item_id):
  with db() as cursor:
    if request.method == 'PUT':
      data = request.json or {}
      item_type = clean_item_type(data.get('item_type'), ITEM_TYPES_STOCK)
      recipe_id = to_int_or_none(data.get('recipe_id')) if item_type == 'recipe' else None
      cursor.execute(
          """
          UPDATE inventory
          SET name = %s, quantity = %s, location = %s, expiry_date = %s,
              item_type = %s, recipe_id = %s
          WHERE id = %s
          """,
          (
              data.get('name'),
              data.get('quantity'),
              data.get('location'),
              data.get('expiry_date'),
              item_type,
              recipe_id,
              item_id,
          ),
      )
    else:
      cursor.execute('DELETE FROM inventory WHERE id = %s', (item_id,))
  return jsonify({'status': 'success'})


@app.route('/api/inventory/<int:item_id>/finish', methods=['POST'])
def finish_inventory(item_id):
  """Marque un article de stock comme terminé et réapprovisionne la liste de courses.

  Corps attendu : {"restock": [{"name": "...", "quantity": "...", "item_type": "..."}]}
  Les articles déjà présents (non achetés) dans la liste de courses ne sont pas dupliqués.
  """
  data = request.json or {}
  restock = data.get('restock') or []

  added, skipped = [], []
  with db() as cursor:
    cursor.execute('SELECT lower(name) AS n FROM shopping_list WHERE bought = 0')
    already = {row['n'] for row in cursor.fetchall()}

    for entry in restock:
      name = (entry.get('name') or '').strip()
      if not name:
        continue
      if name.lower() in already:
        skipped.append(name)
        continue
      already.add(name.lower())
      cursor.execute(
          """
          INSERT INTO shopping_list (name, quantity, estimated_price, item_type)
          VALUES (%s, %s, 0, %s)
          """,
          (
              name,
              entry.get('quantity') or '',
              clean_item_type(entry.get('item_type'), ITEM_TYPES_SHOPPING),
          ),
      )
      added.append(name)

    cursor.execute('DELETE FROM inventory WHERE id = %s', (item_id,))

  return jsonify({'status': 'success', 'added': added, 'skipped': skipped})


# --- API : BUDGET ---
@app.route('/api/expenses', methods=['GET', 'POST'])
def handle_expenses():
  if request.method == 'POST':
    data = request.json or {}
    date_str = data.get('date') or datetime.now().strftime('%Y-%m-%d')
    with db() as cursor:
      cursor.execute(
          'INSERT INTO expenses (description, amount, date) VALUES (%s, %s, %s)',
          (data['description'], float(data['amount']), date_str),
      )
    return jsonify({'status': 'success'})

  with db() as cursor:
    cursor.execute('SELECT * FROM expenses ORDER BY date DESC, id DESC')
    rows = cursor.fetchall()
  return jsonify([dict(row) for row in rows])


@app.route('/api/expenses/<int:exp_id>', methods=['PUT', 'DELETE'])
def update_expense(exp_id):
  with db() as cursor:
    if request.method == 'PUT':
      data = request.json or {}
      cursor.execute(
          'UPDATE expenses SET description = %s, amount = %s, date = %s WHERE id = %s',
          (
              data.get('description'),
              float(data.get('amount') or 0),
              data.get('date'),
              exp_id,
          ),
      )
    else:
      cursor.execute('DELETE FROM expenses WHERE id = %s', (exp_id,))
  return jsonify({'status': 'success'})


# --- API : TRI IA DE LA LISTE DE COURSES ---
@app.route('/api/shopping/ai-sort', methods=['POST'])
def ai_sort_shopping():
  with db() as cursor:
    cursor.execute('SELECT * FROM shopping_list WHERE bought = 0')
    rows = cursor.fetchall()

  if not rows:
    return jsonify({})

  items = [dict(row) for row in rows]
  item_names = [item['name'] for item in items]

  prompt = (
      'Classe les articles de courses suivants selon les rayons logiques'
      " d'un supermarché (ex: 🍎 Fruits & Légumes, 🧀 Produits Frais,"
      ' 🥫 Épicerie & Sec, 🥩 Boucherie, 🧻 Hygiène). Réponds STRICTEMENT'
      ' sous un format JSON où les clés sont les noms des rayons et les valeurs'
      f' sont les listes des articles correspondants.\nArticles : {item_names}'
  )

  try:
    response = client.models.generate_content(
        model='gemini-3.8-flash',
        contents=prompt,
    )
    return jsonify({'result': response.text, 'items': items, 'fallback': False})
  except Exception as e:
    return jsonify({'error': str(e), 'items': items, 'fallback': True})


if __name__ == '__main__':
  init_db()
  app.run(host='0.0.0.0', port=5000, debug=True)
