import os
import random
import re
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_bcrypt import Bcrypt
from datetime import datetime

app = Flask(__name__)
app.secret_key = "chave_super_secreta"

# --- Banco de dados SQLite ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')

# --- Socket & Criptografia ---
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
bcrypt = Bcrypt(app)

# ------------------ Função auxiliar ------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Cria tabelas se não existirem
with get_db_connection() as conn:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            seen BOOLEAN DEFAULT FALSE
        )
    """)
    conn.commit()


# ---------------- ROTAS ----------------

@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('chat'))
    return redirect(url_for('login'))

# -------- CADASTRO --------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')

        # Lista básica de palavras proibidas
        palavras_proibidas = [
            "pênis", "penis", "buceta", "caralho", "porra", "pau", "piroca", 
            "xereca", "bosta", "merda", "cocô", "cu", "rola", "xota", "boquete"
        ]

        # Remove acentuação e coloca em minúsculas para comparar
        def normalizar_nome(nome):
            return re.sub(r'[^a-z0-9]', '', nome.lower())

        nome_normalizado = normalizar_nome(username)

        if any(p in nome_normalizado for p in palavras_proibidas):
            username = f"Usuário {random.randint(100, 999)}"

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ?", (username,))
        if cur.fetchone():
            flash("Usuário já existe!", "warning")
            conn.close()
            return redirect(url_for('register'))

        cur.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
        conn.commit()
        conn.close()

        # Login automático
        session['user_id'] = cur.lastrowid
        session['username'] = username

        # Notifica todos os usuários conectados que um novo usuário entrou
        socketio.emit("user_joined", {"id": session['user_id'], "username": username})

        return redirect(url_for('chat'))


    return render_template('register.html')

# -------- LOGIN --------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = cur.fetchone()
        conn.close()

        if user and bcrypt.check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('chat'))
        else:
            flash("Usuário ou senha incorretos.", "danger")

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Você saiu da conta.", "info")
    return redirect(url_for('login'))

# -------- CHAT PRINCIPAL --------
@app.route('/chat')
def chat():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username FROM users WHERE id != ?", (session['user_id'],))
    users = cur.fetchall()
    conn.close()

    return render_template('chat.html', username=session['username'], users=users)

# -------- API DE MENSAGENS --------
@app.route('/messages/<int:receiver_id>')
def get_messages(receiver_id):
    if 'user_id' not in session:
        return jsonify([])

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT sender_id, text, created_at, seen FROM messages
        WHERE (sender_id = ? AND receiver_id = ?)
           OR (sender_id = ? AND receiver_id = ?)
        ORDER BY created_at ASC
    """, (session['user_id'], receiver_id, receiver_id, session['user_id']))
    messages = cur.fetchall()
    conn.close()

    return jsonify([dict(row) for row in messages])

@app.route('/unread_counts')
def unread_counts():
    if 'user_id' not in session:
        return jsonify({})
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT sender_id, COUNT(*) as count FROM messages
        WHERE receiver_id = ? AND seen = 0
        GROUP BY sender_id
    """, (session['user_id'],))
    counts = {row['sender_id']: row['count'] for row in cur.fetchall()}
    conn.close()
    return jsonify(counts)

# ---------------- SOCKET.IO ----------------
@socketio.on("join")
def handle_join(data):
    user_id = data.get("user_id") or session.get("user_id")
    if not user_id:
        return
    join_room(str(user_id))
    print(f"✅ Usuário {user_id} entrou na sala {user_id}")

@socketio.on("send_message")
def handle_send_message(data):
    message = data.get("message")
    receiver_id = data.get("receiver_id")
    sender_id = session.get("user_id")
    sender_name = session.get("username")
    timestamp = datetime.now()

    if not all([message, receiver_id, sender_id]):
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (sender_id, receiver_id, text, created_at, seen)
        VALUES (?, ?, ?, ?, 0)
    """, (sender_id, receiver_id, message, timestamp))
    conn.commit()
    conn.close()

    emit(
        "receive_message",
        {
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message": message,
            "receiver_id": receiver_id
        },
        room=str(receiver_id),
    )

@socketio.on("mark_as_read")
def mark_as_read(data):
    sender_id = data.get("sender_id")
    receiver_id = session.get("user_id")
    if not all([sender_id, receiver_id]):
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE messages
        SET seen = 1
        WHERE sender_id = ? AND receiver_id = ? AND seen = 0
    """, (sender_id, receiver_id))
    conn.commit()
    conn.close()

@socketio.on("typing")
def on_typing(data):
    receiver_id = data.get("receiver_id")
    sender_id = session.get("user_id")
    sender_name = session.get("username")
    if receiver_id and sender_id:
        emit("typing", {"sender_id": sender_id, "sender_name": sender_name}, room=str(receiver_id))

@socketio.on("stop_typing")
def on_stop_typing(data):
    receiver_id = data.get("receiver_id")
    sender_id = session.get("user_id")
    if receiver_id and sender_id:
        emit("stop_typing", {"sender_id": sender_id}, room=str(receiver_id))


# ---------------- MAIN ----------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)
