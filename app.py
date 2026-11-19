from datetime import datetime
import os
from authlib.integrations.flask_client import OAuth
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
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


def get_db_connection():
  conn = psycopg2.connect(
      DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
  )
  return conn


def init_db():
  conn = get_db_connection()
  cursor = conn.cursor()

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS shopping_list (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            quantity TEXT,
            estimated_price REAL DEFAULT 0.0,
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
  conn.commit()
  cursor.close()
  conn.close()


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
  conn = get_db_connection()
  cursor = conn.cursor()
  if request.method == 'POST':
    data = request.json
    cursor.execute(
        'INSERT INTO shopping_list (name, quantity, estimated_price) VALUES'
        ' (%s, %s, %s)',
        (
            data.get('name', ''),
            data.get('quantity', ''),
            float(data.get('estimated_price') or 0),
        ),
    )
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})
  else:
    cursor.execute('SELECT * FROM shopping_list ORDER BY bought ASC, id DESC')
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route('/api/shopping/<int:item_id>', methods=['PUT', 'DELETE'])
def update_shopping(item_id):
  conn = get_db_connection()
  cursor = conn.cursor()
  if request.method == 'PUT':
    data = request.json
    if 'bought' in data and len(data) == 1:
      cursor.execute(
          'UPDATE shopping_list SET bought = %s WHERE id = %s',
          (1 if data['bought'] else 0, item_id),
      )
    else:
      cursor.execute(
          'UPDATE shopping_list SET name = %s, quantity = %s, estimated_price ='
          ' %s, bought = %s WHERE id = %s',
          (
              data.get('name'),
              data.get('quantity'),
              float(data.get('estimated_price') or 0),
              1 if data.get('bought') else 0,
              item_id,
          ),
      )
  elif request.method == 'DELETE':
    cursor.execute('DELETE FROM shopping_list WHERE id = %s', (item_id,))
  conn.commit()
  cursor.close()
  conn.close()
  return jsonify({'status': 'success'})

# --- API : RECETTES ---

@app.route('/api/recipes', methods=['GET'])
def get_recipes():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM recipes
        ORDER BY name
    """)

    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    return jsonify([dict(row) for row in rows])


@app.route('/api/recipes', methods=['POST'])
def create_recipe():
    data = request.json

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO recipes (name)
        VALUES (%s)
        RETURNING id
        """,
        (data['name'],)
    )

    recipe_id = cursor.fetchone()['id']

    for ingredient in data.get('ingredients', []):
        cursor.execute(
            """
            INSERT INTO recipe_ingredients (
                recipe_id,
                ingredient_name
            )
            VALUES (%s, %s)
            """,
            (recipe_id, ingredient)
        )

    conn.commit()

    cursor.close()
    conn.close()

    return jsonify({'status': 'success'})


@app.route('/api/recipes/<int:recipe_id>/ingredients')
def get_recipe_ingredients(recipe_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT ingredient_name
        FROM recipe_ingredients
        WHERE recipe_id = %s
        ORDER BY ingredient_name
        """,
        (recipe_id,)
    )

    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    return jsonify([row['ingredient_name'] for row in rows])

# --- API : INVENTAIRE ---
@app.route('/api/inventory', methods=['GET', 'POST'])
def handle_inventory():
  conn = get_db_connection()
  cursor = conn.cursor()
  if request.method == 'POST':
    data = request.json
    cursor.execute(
    '''
    INSERT INTO inventory (
        name,
        quantity,
        location,
        expiry_date,
        item_type,
        recipe_id
    )
    VALUES (%s, %s, %s, %s, %s, %s)
    ''',
    (
        data.get('name', ''),
        data.get('quantity', ''),
        data.get('location', 'Frigo'),
        data.get('expiry_date', ''),
        data.get('item_type', 'ingredient'),
        data.get('recipe_id'),
    ),
)
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})
  else:
    cursor.execute('SELECT * FROM inventory ORDER BY location, id DESC')
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route('/api/inventory/<int:item_id>', methods=['PUT', 'DELETE'])
def update_inventory(item_id):
  conn = get_db_connection()
  cursor = conn.cursor()
  if request.method == 'PUT':
    data = request.json
    cursor.execute(
    '''
    UPDATE inventory
    SET
        name = %s,
        quantity = %s,
        location = %s,
        expiry_date = %s,
        item_type = %s,
        recipe_id = %s
    WHERE id = %s
    ''',
    (
        data.get('name'),
        data.get('quantity'),
        data.get('location'),
        data.get('expiry_date'),
        data.get('item_type', 'ingredient'),
        data.get('recipe_id'),
        item_id,
    ),
)
  elif request.method == 'DELETE':
    cursor.execute('DELETE FROM inventory WHERE id = %s', (item_id,))
  conn.commit()
  cursor.close()
  conn.close()
  return jsonify({'status': 'success'})


# --- API : BUDGET ---
@app.route('/api/expenses', methods=['GET', 'POST'])
def handle_expenses():
  conn = get_db_connection()
  cursor = conn.cursor()
  if request.method == 'POST':
    data = request.json
    date_str = data.get('date') or datetime.now().strftime('%Y-%m-%d')
    cursor.execute(
        'INSERT INTO expenses (description, amount, date) VALUES (%s, %s, %s)',
        (data['description'], float(data['amount']), date_str),
    )
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'status': 'success'})
  else:
    cursor.execute('SELECT * FROM expenses ORDER BY date DESC, id DESC')
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route('/api/expenses/<int:exp_id>', methods=['PUT', 'DELETE'])
def update_expense(exp_id):
  conn = get_db_connection()
  cursor = conn.cursor()
  if request.method == 'PUT':
    data = request.json
    cursor.execute(
        'UPDATE expenses SET description = %s, amount = %s, date = %s WHERE id ='
        ' %s',
        (
            data.get('description'),
            float(data.get('amount') or 0),
            data.get('date'),
            exp_id,
        ),
    )
  elif request.method == 'DELETE':
    cursor.execute('DELETE FROM expenses WHERE id = %s', (exp_id,))
  conn.commit()
  cursor.close()
  conn.close()
  return jsonify({'status': 'success'})

import os
from google import genai

# Remplace 'TA_CLE_API' par ta vraie clé secrète
client = genai.Client(
    api_key=os.environ['GEMINI_API_KEY']
)


@app.route('/api/shopping/ai-sort', methods=['POST'])
def ai_sort_shopping():
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute('SELECT * FROM shopping_list WHERE bought = 0')
  rows = cursor.fetchall()
  cursor.close()
  conn.close()

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
    return jsonify({
        'error': str(e),
        'items': items,
        'fallback': True
    })
    
    
if __name__ == '__main__':
  init_db()
  app.run(host='0.0.0.0', port=5000, debug=True)