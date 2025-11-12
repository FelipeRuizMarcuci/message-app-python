import os
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_bcrypt import Bcrypt
from datetime import datetime
from models import mysql

app = Flask(__name__)
app.secret_key = "chave_super_secreta"

# Configuração MySQL (XAMPP)
app.config['MYSQL_HOST'] = os.getenv('DB_HOST')
app.config['MYSQL_USER'] = os.getenv('DB_USER')
app.config['MYSQL_PASSWORD'] = os.getenv('DB_PASSWORD')
app.config['MYSQL_DB'] = os.getenv('DB_NAME')

mysql.init_app(app)
bcrypt = Bcrypt(app)
socketio = SocketIO(app, cors_allowed_origins="*")

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
        username = request.form['username']
        password = request.form['password']
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')

        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM users WHERE username = %s", [username])
        if cur.fetchone():
            flash("Usuário já existe!", "warning")
            return redirect(url_for('register'))

        cur.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, hashed_pw))
        mysql.connection.commit()
        cur.close()

        flash("Cadastro realizado com sucesso! Faça login.", "success")
        return redirect(url_for('login'))

    return render_template('register.html')

# -------- LOGIN --------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM users WHERE username = %s", [username])
        user = cur.fetchone()

        if user and bcrypt.check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['username'] = user[1]
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

    cur = mysql.connection.cursor()
    cur.execute("SELECT id, username FROM users WHERE id != %s", [session['user_id']])
    users = cur.fetchall()
    cur.close()

    return render_template('chat.html', username=session['username'], users=users)

# -------- API DE MENSAGENS --------
@app.route('/messages/<int:receiver_id>')
def get_messages(receiver_id):
    if 'user_id' not in session:
        return jsonify([])

    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT sender_id, text, created_at, seen FROM messages
        WHERE (sender_id = %s AND receiver_id = %s)
           OR (sender_id = %s AND receiver_id = %s)
        ORDER BY created_at ASC
    """, (session['user_id'], receiver_id, receiver_id, session['user_id']))
    messages = cur.fetchall()
    cur.close()

    return jsonify(messages)

@app.route('/unread_counts')
def unread_counts():
    if 'user_id' not in session:
        return jsonify({})
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT sender_id, COUNT(*) FROM messages
        WHERE receiver_id = %s AND seen = FALSE
        GROUP BY sender_id
    """, [session['user_id']])
    counts = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    return jsonify(counts)

# ---------------- SOCKET.IO ----------------
@socketio.on("join")
def handle_join(data):
    # usuario entra na própria sala (room = user_id)
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

    # Salvar mensagem no banco
    cur = mysql.connection.cursor()
    cur.execute("""
        INSERT INTO messages (sender_id, receiver_id, text, created_at, seen)
        VALUES (%s, %s, %s, %s, FALSE)
    """, (sender_id, receiver_id, message, timestamp))
    mysql.connection.commit()
    cur.close()

    # Emitir para o destinatário (somente para o receiver)
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
    cur = mysql.connection.cursor()
    cur.execute("""
        UPDATE messages
        SET seen = TRUE
        WHERE sender_id = %s AND receiver_id = %s AND seen = FALSE
    """, (sender_id, receiver_id))
    mysql.connection.commit()
    cur.close()

# ---------- Typing indicator ----------
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
if __name__ == '__main__':
    socketio.run(app, debug=True)
