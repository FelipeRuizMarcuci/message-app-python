import os
import random
import re
from datetime import datetime
from collections import defaultdict
from threading import Lock

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)
from flask_socketio import SocketIO, emit, join_room
from flask_bcrypt import Bcrypt
from werkzeug.utils import secure_filename

from sqlalchemy import func, or_, and_
from sqlalchemy.exc import IntegrityError

from models import db, User, Message

# ---------------- APP ----------------
app = Flask(__name__)
app.secret_key = "chave_super_secreta"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")

app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
bcrypt = Bcrypt(app)

# ---------------- PRESENCE (ONLINE/OFFLINE) ----------------
online_users = set()
sid_to_user = {}
user_to_sids = defaultdict(set)
presence_lock = Lock()

with app.app_context():
    db.create_all()

print(User.__table__.columns.keys())

# ---------------- UPLOADS ----------------
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------- HELPERS ----------------
def load_prohibited_words():
    whitelist_path = os.path.join(BASE_DIR, "whitelist.txt")
    if os.path.exists(whitelist_path):
        with open(whitelist_path, "r", encoding="utf-8") as f:
            return [linha.strip().lower() for linha in f if linha.strip()]
    return []


def username_filter_with_whitelist(username: str) -> str:
    palavras_proibidas = load_prohibited_words()

    substituicoes = {
        "a": "[a@4ÀÁÂÃÄÅàáâãäå]",
        "e": "[e3ÈÉÊËèéêë]",
        "i": "[i1!ÌÍÎÏìíîï]",
        "o": "[o0ÒÓÔÕÖòóôõö]",
        "u": "[uùúûüÙÚÛÜ]",
        "c": "[cçÇ]",
        "s": "[s5$]",
        "t": "[t7+]",
        "b": "[b8]",
        "g": "[g9]",
        "z": "[z2]",
    }

    def gerar_regex(palavra: str):
        regex = ""
        for char in palavra:
            regex += substituicoes.get(char.lower(), re.escape(char.lower()))
        return re.compile(regex, re.IGNORECASE)

    padroes = [gerar_regex(p) for p in palavras_proibidas if len(p) > 2]
    nome_limpo = (username or "").lower()

    contem_proibida = any(p.search(nome_limpo) for p in padroes)
    if contem_proibida:
        return f"Usuário {random.randint(100, 999)}"

    return username


def user_is_online(user_id: int) -> bool:
    with presence_lock:
        return int(user_id) in online_users


def message_status_for_user(message, viewer_id: int) -> str:
    """
    Status retornado para o frontend.
    - read: mensagem visualizada
    - delivered: destinatário online (aproximação para histórico)
    - sent: apenas enviada
    """
    if bool(message.seen):
        return "read"

    # só faz sentido mostrar status especial para mensagens que eu enviei
    if int(message.sender_id) == int(viewer_id):
        if user_is_online(int(message.receiver_id)):
            return "delivered"

    return "sent"


@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()


# ---------------- ROTAS ----------------
@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("chat"))
    return redirect(url_for("login"))


# -------- CADASTRO --------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not username or not email or not password or not confirm_password:
            flash("Preencha todos os campos.", "warning")
            return redirect(url_for("register"))

        if not EMAIL_RE.match(email):
            flash("E-mail inválido.", "warning")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("As senhas não conferem.", "danger")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.", "warning")
            return redirect(url_for("register"))

        username = username_filter_with_whitelist(username)

        exists = User.query.filter(
            or_(User.username == username, User.email == email)
        ).first()
        if exists:
            if exists.username == username:
                flash("Usuário já existe!", "warning")
            else:
                flash("E-mail já está em uso!", "warning")
            return redirect(url_for("register"))

        hashed_pw = bcrypt.generate_password_hash(password).decode("utf-8")

        user = User(
            username=username,
            email=email,
            password=hashed_pw,
            display_name=username,
            status_text="",
            avatar_url=None,
        )

        try:
            db.session.add(user)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Não foi possível cadastrar. Tente outro usuário/e-mail.", "danger")
            return redirect(url_for("register"))

        session["user_id"] = user.id
        session["username"] = user.username
        session["email"] = user.email

        socketio.emit(
            "user_joined",
            {
                "id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
                "status_text": user.status_text or "",
            },
        )

        return redirect(url_for("chat"))

    return render_template("register.html")


# -------- LOGIN --------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()

        if user and bcrypt.check_password_hash(user.password, password):
            session["user_id"] = user.id
            session["username"] = user.username
            session["email"] = user.email
            return redirect(url_for("chat"))
        else:
            flash("Usuário ou senha incorretos.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Você saiu da conta.", "info")
    return redirect(url_for("login"))


# -------- CHAT PRINCIPAL --------
@app.route("/chat")
def chat():
    if "user_id" not in session:
        return redirect(url_for("login"))

    my_id = int(session["user_id"])
    me = User.query.get(my_id)
    users = User.query.filter(User.id != my_id).all()

    return render_template(
        "chat.html",
        username=session.get("username", ""),
        users=users,
        me=me,
    )


# -------- META DA LISTA (preview + hora) --------
@app.route("/contacts_meta")
def contacts_meta():
    if "user_id" not in session:
        return jsonify({})

    my_id = int(session["user_id"])
    others = User.query.filter(User.id != my_id).all()
    meta = {}

    for u in others:
        last = (
            Message.query.filter(
                or_(
                    and_(Message.sender_id == my_id, Message.receiver_id == u.id),
                    and_(Message.sender_id == u.id, Message.receiver_id == my_id),
                )
            )
            .order_by(Message.created_at.desc())
            .first()
        )

        meta[u.id] = {
            "last_text": (last.text if last else ""),
            "last_at": (
                last.created_at.isoformat() if last and last.created_at else None
            ),
        }

    return jsonify(meta)


# -------- ONLINE USERS (fallback HTTP) --------
@app.route("/online_users")
def online_users_api():
    with presence_lock:
        return jsonify(list(online_users))


# -------- PERFIL --------
@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = int(session["user_id"])
    me = User.query.get(user_id)

    if not me:
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        status_text = request.form.get("status_text", "").strip()

        file = request.files.get("avatar")
        avatar_url = None

        if file and file.filename:
            if not allowed_file(file.filename):
                flash("Formato inválido. Use PNG/JPG/JPEG/WEBP.", "warning")
                return redirect(url_for("profile"))

            filename = secure_filename(file.filename)
            ext = filename.rsplit(".", 1)[1].lower()
            final_name = f"user_{user_id}.{ext}"
            save_path = os.path.join(UPLOAD_FOLDER, final_name)
            file.save(save_path)
            avatar_url = f"/static/uploads/{final_name}"

        if display_name:
            me.display_name = display_name
        me.status_text = status_text

        if avatar_url is not None:
            me.avatar_url = avatar_url

        db.session.commit()
        flash("Perfil atualizado com sucesso!", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html", me=me)


# -------- API DE MENSAGENS --------
@app.route("/messages/<int:receiver_id>")
def get_messages(receiver_id):
    if "user_id" not in session:
        return jsonify([])

    my_id = int(session["user_id"])

    msgs = (
        Message.query.filter(
            ((Message.sender_id == my_id) & (Message.receiver_id == receiver_id))
            | ((Message.sender_id == receiver_id) & (Message.receiver_id == my_id))
        )
        .order_by(Message.created_at.asc())
        .all()
    )

    return jsonify(
        [
            {
                "id": int(m.id),
                "sender_id": int(m.sender_id),
                "receiver_id": int(m.receiver_id),
                "text": m.text,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "seen": bool(m.seen),
                "status": message_status_for_user(m, my_id),
            }
            for m in msgs
        ]
    )


@app.route("/unread_counts")
def unread_counts():
    if "user_id" not in session:
        return jsonify({})

    my_id = int(session["user_id"])

    rows = (
        db.session.query(Message.sender_id, func.count(Message.id))
        .filter(Message.receiver_id == my_id, Message.seen == False)  # noqa: E712
        .group_by(Message.sender_id)
        .all()
    )

    return jsonify({int(sender_id): int(count) for sender_id, count in rows})


# ---------------- SOCKET.IO ----------------
@socketio.on("join")
def handle_join(data):
    user_id = data.get("user_id") or session.get("user_id")
    if not user_id:
        return

    user_id = int(user_id)
    sid = request.sid

    with presence_lock:
        sid_to_user[sid] = user_id
        was_online = user_id in online_users
        user_to_sids[user_id].add(sid)
        online_users.add(user_id)

    join_room(str(user_id))

    if not was_online:
        socketio.emit("presence", {"user_id": user_id, "online": True})

    emit("online_list", {"online": list(online_users)})


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid

    with presence_lock:
        uid = sid_to_user.pop(sid, None)
        if uid is None:
            return

        sids = user_to_sids.get(uid)
        if sids and sid in sids:
            sids.remove(sid)

        if not sids:
            user_to_sids.pop(uid, None)
            if uid in online_users:
                online_users.discard(uid)
                socketio.emit("presence", {"user_id": uid, "online": False})


@socketio.on("send_message")
def handle_send_message(data):
    message_text = (data.get("message") or "").strip()
    receiver_id = data.get("receiver_id")
    sender_id = session.get("user_id")
    sender_name = session.get("username") or ""
    temp_id = data.get("temp_id")

    if not all([message_text, receiver_id, sender_id]):
        return

    try:
        receiver_id = int(receiver_id)
        sender_id = int(sender_id)
    except Exception:
        return

    msg = Message(
        sender_id=sender_id,
        receiver_id=receiver_id,
        text=message_text,
        created_at=datetime.utcnow(),
        seen=False,
    )
    db.session.add(msg)
    db.session.commit()

    payload = {
        "id": int(msg.id),
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message": message_text,
        "receiver_id": receiver_id,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "seen": False,
    }

    # Confirma ao remetente que a mensagem foi salva/enviada
    socketio.emit(
        "message_sent",
        {
            "temp_id": temp_id,
            "message_id": int(msg.id),
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        },
        room=str(sender_id),
    )

    # Entrega ao destinatário
    socketio.emit("receive_message", payload, room=str(receiver_id))

    # Se o destinatário está online, marca como entregue visualmente no remetente
    if user_is_online(receiver_id):
        socketio.emit(
            "message_delivered",
            {
                "message_id": int(msg.id),
            },
            room=str(sender_id),
        )


@socketio.on("mark_as_read")
def mark_as_read(data):
    sender_id = data.get("sender_id")
    receiver_id = session.get("user_id")

    if not all([sender_id, receiver_id]):
        return

    try:
        sender_id = int(sender_id)
        receiver_id = int(receiver_id)
    except Exception:
        return

    unread_messages = (
        Message.query.filter_by(
            sender_id=sender_id,
            receiver_id=receiver_id,
            seen=False,
        )
        .order_by(Message.id.asc())
        .all()
    )

    if not unread_messages:
        return

    message_ids = [int(m.id) for m in unread_messages]

    for msg in unread_messages:
        msg.seen = True

    db.session.commit()

    # Notifica o remetente original que essas mensagens foram visualizadas
    socketio.emit(
        "messages_read",
        {
            "message_ids": message_ids,
            "reader_id": receiver_id,
        },
        room=str(sender_id),
    )


@socketio.on("typing")
def on_typing(data):
    receiver_id = data.get("receiver_id")
    sender_id = session.get("user_id")
    sender_name = session.get("username")
    if receiver_id and sender_id:
        emit(
            "typing",
            {"sender_id": int(sender_id), "sender_name": sender_name},
            room=str(receiver_id),
        )


@socketio.on("stop_typing")
def on_stop_typing(data):
    receiver_id = data.get("receiver_id")
    sender_id = session.get("user_id")
    if receiver_id and sender_id:
        emit("stop_typing", {"sender_id": int(sender_id)}, room=str(receiver_id))


# ---------------- CHAMADAS (WebRTC signaling) ----------------
@socketio.on("call_offer")
def on_call_offer(data):
    to = data.get("to")
    if not to:
        return
    socketio.emit("call_offer", data, room=str(to))


@socketio.on("call_answer")
def on_call_answer(data):
    to = data.get("to")
    if not to:
        return
    socketio.emit("call_answer", data, room=str(to))


@socketio.on("ice_candidate")
def on_ice_candidate(data):
    to = data.get("to")
    if not to:
        return
    socketio.emit("ice_candidate", data, room=str(to))


@socketio.on("hangup")
def on_hangup(data):
    to = data.get("to")
    if not to:
        return
    socketio.emit("hangup", data, room=str(to))


# ---------------- MAIN ----------------
if __name__ == "__main__":
    port = 5000
    socketio.run(app, host="127.0.0.1", port=port)