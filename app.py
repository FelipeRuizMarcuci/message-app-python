import os
import json
import random
import re
import uuid
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
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_bcrypt import Bcrypt
from werkzeug.utils import secure_filename

from sqlalchemy import func, or_, and_
from sqlalchemy.exc import IntegrityError

from models import db, User, Message, Group, GroupMember, GroupMessage, GroupRead

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

# ---------------- PRESENCE ----------------
online_users = set()
sid_to_user = {}
user_to_sids = defaultdict(set)
presence_lock = Lock()

# ---------------- CALLS ----------------
active_group_calls = defaultdict(set)
group_call_lock = Lock()

with app.app_context():
    db.create_all()

# ---------------- UPLOADS ----------------
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
CHAT_UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "chat_uploads")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CHAT_UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
ALLOWED_CHAT_FILE_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "webp",
    "gif",
    "pdf",
    "txt",
    "zip",
    "rar",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "mp4",
    "webm",
    "mp3",
    "wav",
    "ogg",
    "m4a",
}

CHAT_JSON_PREFIX = "__CHATJSON__::"


# ---------------- HELPERS ----------------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_chat_file(filename: str) -> bool:
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_CHAT_FILE_EXTENSIONS
    )


def group_room_name(group_id):
    return f"group_call_{int(group_id)}"


def user_is_online(user_id: int) -> bool:
    with presence_lock:
        return int(user_id) in online_users


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


def serialize_message_payload(
    kind="text",
    text="",
    file_url=None,
    file_name=None,
    file_mime=None,
    edited=False,
    deleted=False,
):
    payload = {
        "kind": kind or "text",
        "text": text or "",
        "file_url": file_url,
        "file_name": file_name,
        "file_mime": file_mime,
        "edited": bool(edited),
        "deleted": bool(deleted),
    }
    return CHAT_JSON_PREFIX + json.dumps(payload, ensure_ascii=False)


def deserialize_message_payload(raw_text: str):
    if not raw_text:
        return {
            "kind": "text",
            "text": "",
            "file_url": None,
            "file_name": None,
            "file_mime": None,
            "edited": False,
            "deleted": False,
            "is_image": False,
            "is_audio": False,
        }

    if isinstance(raw_text, str) and raw_text.startswith(CHAT_JSON_PREFIX):
        try:
            payload = json.loads(raw_text[len(CHAT_JSON_PREFIX):])
            mime = payload.get("file_mime") or ""
            kind = payload.get("kind") or "text"
            is_image = kind == "image" or str(mime).startswith("image/")
            is_audio = kind == "audio" or str(mime).startswith("audio/")
            return {
                "kind": kind,
                "text": payload.get("text") or "",
                "file_url": payload.get("file_url"),
                "file_name": payload.get("file_name"),
                "file_mime": payload.get("file_mime"),
                "edited": bool(payload.get("edited")),
                "deleted": bool(payload.get("deleted")),
                "is_image": is_image,
                "is_audio": is_audio,
            }
        except Exception:
            pass

    return {
        "kind": "text",
        "text": raw_text,
        "file_url": None,
        "file_name": None,
        "file_mime": None,
        "edited": False,
        "deleted": False,
        "is_image": False,
        "is_audio": False,
    }


def serialize_group(group: Group):
    members = (
        db.session.query(GroupMember.user_id)
        .filter(GroupMember.group_id == group.id)
        .order_by(GroupMember.user_id.asc())
        .all()
    )
    return {
        "id": int(group.id),
        "name": group.name,
        "avatar_url": group.avatar_url or "/static/uploads/default.png",
        "description": group.description or "",
        "created_by": int(group.created_by),
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "members": [int(m.user_id) for m in members],
    }


def get_group_by_id(group_id):
    try:
        group_id = int(group_id)
    except Exception:
        return None
    return Group.query.get(group_id)


def user_groups(user_id):
    try:
        user_id = int(user_id)
    except Exception:
        return []

    groups = (
        db.session.query(Group)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(GroupMember.user_id == user_id)
        .order_by(Group.created_at.desc())
        .all()
    )
    return [serialize_group(g) for g in groups]


def user_in_group(user_id, group_id):
    try:
        user_id = int(user_id)
        group_id = int(group_id)
    except Exception:
        return False

    return (
        GroupMember.query.filter_by(user_id=user_id, group_id=group_id).first()
        is not None
    )


def get_group_members(group_id):
    try:
        group_id = int(group_id)
    except Exception:
        return []

    rows = (
        db.session.query(GroupMember.user_id)
        .filter(GroupMember.group_id == group_id)
        .order_by(GroupMember.user_id.asc())
        .all()
    )
    return [int(r.user_id) for r in rows]


def get_or_create_group_read(group_id: int, user_id: int):
    group_read = GroupRead.query.filter_by(group_id=group_id, user_id=user_id).first()
    if not group_read:
        group_read = GroupRead(
            group_id=group_id,
            user_id=user_id,
            last_read_message_id=None,
            updated_at=datetime.utcnow(),
        )
        db.session.add(group_read)
    return group_read


def build_private_message_response(message, viewer_id: int, target_id=None):
    payload = deserialize_message_payload(message.text)

    status = "sent"
    if bool(message.seen):
        status = "read"
    elif int(message.sender_id) == int(viewer_id) and user_is_online(int(message.receiver_id)):
        status = "delivered"

    return {
        "id": int(message.id),
        "sender_id": int(message.sender_id),
        "receiver_id": int(message.receiver_id),
        "text": payload["text"],
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "seen": bool(message.seen),
        "status": status,
        "kind": payload["kind"],
        "edited": payload["edited"],
        "deleted": payload["deleted"],
        "file_url": payload["file_url"],
        "file_name": payload["file_name"],
        "file_mime": payload["file_mime"],
        "is_image": payload["is_image"],
        "is_audio": payload["is_audio"],
        "conversation_type": "user",
        "target_id": target_id,
    }


def build_group_message_response(message, viewer_id: int, target_id=None):
    payload = deserialize_message_payload(message.text)

    return {
        "id": int(message.id),
        "sender_id": int(message.sender_id),
        "receiver_id": -int(message.group_id),
        "text": payload["text"],
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "seen": False,
        "status": "sent",
        "kind": payload["kind"],
        "edited": payload["edited"],
        "deleted": payload["deleted"],
        "file_url": payload["file_url"] or message.file_url,
        "file_name": payload["file_name"],
        "file_mime": payload["file_mime"],
        "is_image": payload["is_image"],
        "is_audio": payload["is_audio"],
        "conversation_type": "group",
        "target_id": target_id,
    }


def preview_from_text(raw_text: str):
    payload = deserialize_message_payload(raw_text)
    if payload["deleted"]:
        return "Mensagem apagada"
    if payload["is_audio"]:
        return "🎵 Áudio"
    if payload["is_image"]:
        return "📷 Imagem"
    if payload["kind"] == "file":
        return f"📎 {payload['file_name'] or 'Arquivo'}"
    return payload["text"] or ""


@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()


# ---------------- ROTAS ----------------
@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("chat"))
    return redirect(url_for("login"))


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

        session["user_id"] = int(user.id)
        session["username"] = str(user.username or "")
        session["email"] = str(user.email or "")

        socketio.emit(
            "user_joined",
            {
                "id": int(user.id),
                "username": user.username,
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
                "status_text": user.status_text or "",
            },
        )

        return redirect(url_for("chat"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()

        if user and bcrypt.check_password_hash(user.password, password):
            session["user_id"] = int(user.id)
            session["username"] = str(user.username or "")
            session["email"] = str(user.email or "")
            return redirect(url_for("chat"))
        else:
            flash("Usuário ou senha incorretos.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Você saiu da conta.", "info")
    return redirect(url_for("login"))


@app.route("/chat")
def chat():
    if "user_id" not in session:
        return redirect(url_for("login"))

    my_id = int(session["user_id"])
    me = User.query.get(my_id)
    users = User.query.filter(User.id != my_id).order_by(User.display_name.asc()).all()
    groups = user_groups(my_id)

    return render_template(
        "chat.html",
        username=session.get("username", ""),
        users=users,
        me=me,
        groups=groups,
    )


@app.route("/contacts_meta")
def contacts_meta():
    if "user_id" not in session:
        return jsonify({})

    my_id = int(session["user_id"])
    others = User.query.filter(User.id != my_id).all()
    groups = user_groups(my_id)
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

        meta[f"user_{u.id}"] = {
            "last_text": preview_from_text(last.text) if last else "",
            "last_at": (last.created_at.isoformat() if last and last.created_at else None),
        }

    for g in groups:
        gid = int(g["id"])
        last = (
            GroupMessage.query.filter(GroupMessage.group_id == gid)
            .order_by(GroupMessage.created_at.desc())
            .first()
        )
        meta[f"group_{gid}"] = {
            "last_text": preview_from_text(last.text) if last else "",
            "last_at": (last.created_at.isoformat() if last and last.created_at else None),
        }

    return jsonify(meta)


@app.route("/online_users")
def online_users_api():
    with presence_lock:
        return jsonify(list(online_users))


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


@app.route("/create_group", methods=["POST"])
def create_group():
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "Não autenticado"}), 401

    my_id = int(session["user_id"])
    data = request.get_json(silent=True) or request.form

    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    avatar_url = (data.get("avatar_url") or "").strip() or "/static/uploads/default.png"
    member_ids = data.get("member_ids") or []

    if isinstance(member_ids, str):
        try:
            member_ids = json.loads(member_ids)
        except Exception:
            member_ids = []

    if not name:
        return jsonify({"ok": False, "error": "Nome do grupo é obrigatório"}), 400

    clean_members = set()
    for mid in member_ids:
        try:
            clean_members.add(int(mid))
        except Exception:
            pass

    clean_members.add(my_id)

    valid_users = {
        int(u.id)
        for u in User.query.filter(User.id.in_(list(clean_members))).all()
    }
    valid_users.add(my_id)

    try:
        group = Group(
            name=name,
            created_by=my_id,
            avatar_url=avatar_url,
            description=description or None,
            created_at=datetime.utcnow(),
        )
        db.session.add(group)
        db.session.flush()

        db.session.add(
            GroupMember(
                group_id=int(group.id),
                user_id=my_id,
                role="admin",
                joined_at=datetime.utcnow(),
            )
        )

        for uid in sorted(valid_users):
            if int(uid) == my_id:
                continue
            db.session.add(
                GroupMember(
                    group_id=int(group.id),
                    user_id=int(uid),
                    role="member",
                    joined_at=datetime.utcnow(),
                )
            )

        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"ok": False, "error": "Não foi possível criar o grupo"}), 400

    group_payload = serialize_group(group)

    for uid in group_payload["members"]:
        socketio.emit("group_created", group_payload, room=str(uid))

    return jsonify({"ok": True, "group": group_payload})


@app.route("/upload_chat_file", methods=["POST"])
def upload_chat_file():
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "Não autenticado"}), 401

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Arquivo não enviado"}), 400

    if not allowed_chat_file(file.filename):
        return jsonify({"ok": False, "error": "Tipo de arquivo não permitido"}), 400

    safe_name = secure_filename(file.filename)
    ext = safe_name.rsplit(".", 1)[1].lower()
    unique_name = f"{session['user_id']}_{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(CHAT_UPLOAD_FOLDER, unique_name)
    file.save(save_path)

    file_url = f"/static/chat_uploads/{unique_name}"
    file_mime = file.mimetype or "application/octet-stream"

    is_image = file_mime.startswith("image/") or ext in {
        "png",
        "jpg",
        "jpeg",
        "webp",
        "gif",
    }
    is_audio = file_mime.startswith("audio/") or ext in {"mp3", "wav", "ogg", "m4a", "webm"}

    if is_audio:
        kind = "audio"
    elif is_image:
        kind = "image"
    else:
        kind = "file"

    return jsonify(
        {
            "ok": True,
            "file_url": file_url,
            "file_name": safe_name,
            "file_mime": file_mime,
            "is_image": is_image,
            "is_audio": is_audio,
            "kind": kind,
        }
    )


@app.route("/messages/<conversation_type>/<int:target_id>")
def get_messages(conversation_type, target_id):
    if "user_id" not in session:
        return jsonify([])

    my_id = int(session["user_id"])

    if conversation_type == "user":
        msgs = (
            Message.query.filter(
                ((Message.sender_id == my_id) & (Message.receiver_id == target_id))
                | ((Message.sender_id == target_id) & (Message.receiver_id == my_id))
            )
            .order_by(Message.created_at.asc())
            .all()
        )
        return jsonify(
            [build_private_message_response(m, my_id, target_id) for m in msgs]
        )

    if conversation_type == "group":
        if not user_in_group(my_id, target_id):
            return jsonify([])

        msgs = (
            GroupMessage.query.filter(GroupMessage.group_id == int(target_id))
            .order_by(GroupMessage.created_at.asc())
            .all()
        )

        out = []
        for m in msgs:
            payload = build_group_message_response(m, my_id, target_id)
            sender = User.query.get(int(m.sender_id))
            payload["sender_name"] = (
                sender.display_name if sender and sender.display_name else sender.username if sender else "Usuário"
            )
            out.append(payload)
        return jsonify(out)

    return jsonify([])


@app.route("/unread_counts")
def unread_counts():
    if "user_id" not in session:
        return jsonify({})

    my_id = int(session["user_id"])
    result = {}

    private_rows = (
        db.session.query(Message.sender_id, func.count(Message.id))
        .filter(Message.receiver_id == my_id, Message.seen == False)  # noqa: E712
        .group_by(Message.sender_id)
        .all()
    )
    for sender_id, count in private_rows:
        result[f"user_{int(sender_id)}"] = int(count)

    for g in user_groups(my_id):
        gid = int(g["id"])
        group_read = GroupRead.query.filter_by(group_id=gid, user_id=my_id).first()
        last_read = (
            int(group_read.last_read_message_id)
            if group_read and group_read.last_read_message_id
            else 0
        )

        count = (
            GroupMessage.query.filter(
                GroupMessage.group_id == gid,
                GroupMessage.id > last_read,
                GroupMessage.sender_id != my_id,
            )
            .count()
        )
        result[f"group_{gid}"] = int(count)

    return jsonify(result)


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
    uid = None

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

    with group_call_lock:
        for gid in list(active_group_calls.keys()):
            if uid in active_group_calls[gid]:
                active_group_calls[gid].discard(uid)
                socketio.emit(
                    "group_call_user_left",
                    {"group_id": gid, "user_id": uid},
                    room=group_room_name(gid),
                )
                if not active_group_calls[gid]:
                    active_group_calls.pop(gid, None)


@socketio.on("send_message")
def handle_send_message(data):
    sender_id = session.get("user_id")
    sender_name = session.get("username") or ""
    temp_id = data.get("temp_id")
    conversation_type = (data.get("conversation_type") or "user").strip().lower()
    target_id = data.get("target_id")

    if not sender_id or not target_id:
        return

    try:
        sender_id = int(sender_id)
        target_id = int(target_id)
    except Exception:
        return

    kind = (data.get("kind") or "text").strip().lower()
    message_text = (data.get("message") or "").strip()
    file_url = data.get("file_url")
    file_name = data.get("file_name")
    file_mime = data.get("file_mime")

    if kind == "text" and not message_text:
        return
    if kind in {"file", "image", "audio"} and not file_url:
        return

    payload_text = serialize_message_payload(
        kind=kind,
        text=message_text,
        file_url=file_url,
        file_name=file_name,
        file_mime=file_mime,
        edited=False,
        deleted=False,
    )

    if conversation_type == "user":
        msg = Message(
            sender_id=sender_id,
            receiver_id=target_id,
            text=payload_text,
            created_at=datetime.utcnow(),
            seen=False,
        )
        db.session.add(msg)
        db.session.commit()

        payload_receiver = build_private_message_response(msg, target_id, target_id)
        payload_receiver["sender_name"] = sender_name

        socketio.emit(
            "message_sent",
            {
                "temp_id": temp_id,
                "message_id": int(msg.id),
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            },
            room=str(sender_id),
        )

        socketio.emit("receive_message", payload_receiver, room=str(target_id))

        if user_is_online(target_id):
            socketio.emit(
                "message_delivered",
                {"message_id": int(msg.id)},
                room=str(sender_id),
            )
        return

    if conversation_type == "group":
        if not user_in_group(sender_id, target_id):
            return

        msg = GroupMessage(
            group_id=target_id,
            sender_id=sender_id,
            text=payload_text,
            file_url=file_url,
            created_at=datetime.utcnow(),
        )
        db.session.add(msg)
        db.session.commit()

        group = get_group_by_id(target_id)
        payload_group = build_group_message_response(msg, sender_id, target_id)
        payload_group["sender_name"] = sender_name
        payload_group["group_name"] = group.name if group else "Grupo"

        socketio.emit(
            "message_sent",
            {
                "temp_id": temp_id,
                "message_id": int(msg.id),
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            },
            room=str(sender_id),
        )

        for member_id in get_group_members(target_id):
            if int(member_id) == int(sender_id):
                continue
            socketio.emit("receive_message", payload_group, room=str(member_id))
        return


@socketio.on("edit_message")
def handle_edit_message(data):
    user_id = session.get("user_id")
    if not user_id:
        return

    message_id = data.get("message_id")
    new_text = (data.get("text") or "").strip()
    conversation_type = (data.get("conversation_type") or "").strip().lower()

    if not message_id or not new_text:
        return

    try:
        message_id = int(message_id)
        user_id = int(user_id)
    except Exception:
        return

    if conversation_type == "group":
        msg = GroupMessage.query.get(message_id)
        if not msg or int(msg.sender_id) != user_id:
            return

        payload = deserialize_message_payload(msg.text)
        if payload["deleted"] or payload["kind"] != "text":
            return

        payload["text"] = new_text
        payload["edited"] = True

        msg.text = serialize_message_payload(
            kind=payload["kind"],
            text=payload["text"],
            file_url=payload["file_url"],
            file_name=payload["file_name"],
            file_mime=payload["file_mime"],
            edited=True,
            deleted=False,
        )
        db.session.commit()

        shared_payload = {
            "message_id": int(msg.id),
            "text": new_text,
            "edited": True,
        }

        for member_id in get_group_members(int(msg.group_id)):
            socketio.emit("message_edited", shared_payload, room=str(member_id))
        return

    msg = Message.query.get(message_id)
    if not msg or int(msg.sender_id) != user_id:
        return

    payload = deserialize_message_payload(msg.text)
    if payload["deleted"] or payload["kind"] != "text":
        return

    payload["text"] = new_text
    payload["edited"] = True

    msg.text = serialize_message_payload(
        kind=payload["kind"],
        text=payload["text"],
        file_url=payload["file_url"],
        file_name=payload["file_name"],
        file_mime=payload["file_mime"],
        edited=True,
        deleted=False,
    )
    db.session.commit()

    shared_payload = {
        "message_id": int(msg.id),
        "text": new_text,
        "edited": True,
    }

    socketio.emit("message_edited", shared_payload, room=str(msg.sender_id))
    socketio.emit("message_edited", shared_payload, room=str(msg.receiver_id))


@socketio.on("delete_message")
def handle_delete_message(data):
    user_id = session.get("user_id")
    if not user_id:
        return

    message_id = data.get("message_id")
    conversation_type = (data.get("conversation_type") or "").strip().lower()

    if not message_id:
        return

    try:
        message_id = int(message_id)
        user_id = int(user_id)
    except Exception:
        return

    if conversation_type == "group":
        msg = GroupMessage.query.get(message_id)
        if not msg or int(msg.sender_id) != user_id:
            return

        payload = deserialize_message_payload(msg.text)
        payload["deleted"] = True
        payload["edited"] = False
        payload["text"] = ""

        msg.text = serialize_message_payload(
            kind=payload["kind"],
            text="",
            file_url=payload["file_url"],
            file_name=payload["file_name"],
            file_mime=payload["file_mime"],
            edited=False,
            deleted=True,
        )
        db.session.commit()

        shared_payload = {
            "message_id": int(msg.id),
            "deleted": True,
        }

        for member_id in get_group_members(int(msg.group_id)):
            socketio.emit("message_deleted", shared_payload, room=str(member_id))
        return

    msg = Message.query.get(message_id)
    if not msg or int(msg.sender_id) != user_id:
        return

    payload = deserialize_message_payload(msg.text)
    payload["deleted"] = True
    payload["edited"] = False
    payload["text"] = ""

    msg.text = serialize_message_payload(
        kind=payload["kind"],
        text="",
        file_url=payload["file_url"],
        file_name=payload["file_name"],
        file_mime=payload["file_mime"],
        edited=False,
        deleted=True,
    )
    db.session.commit()

    shared_payload = {
        "message_id": int(msg.id),
        "deleted": True,
    }

    socketio.emit("message_deleted", shared_payload, room=str(msg.sender_id))
    socketio.emit("message_deleted", shared_payload, room=str(msg.receiver_id))


@socketio.on("mark_as_read")
def mark_as_read(data):
    conversation_type = (data.get("conversation_type") or "user").strip().lower()
    my_id = session.get("user_id")
    if not my_id:
        return

    my_id = int(my_id)

    if conversation_type == "user":
        sender_id = data.get("target_id")
        if not sender_id:
            return
        try:
            sender_id = int(sender_id)
        except Exception:
            return

        unread_messages = (
            Message.query.filter_by(
                sender_id=sender_id,
                receiver_id=my_id,
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

        socketio.emit(
            "messages_read",
            {"message_ids": message_ids, "reader_id": my_id},
            room=str(sender_id),
        )
        return

    if conversation_type == "group":
        gid = data.get("target_id")
        if not gid:
            return
        try:
            gid = int(gid)
        except Exception:
            return

        if not user_in_group(my_id, gid):
            return

        last_msg = (
            GroupMessage.query.filter(GroupMessage.group_id == gid)
            .order_by(GroupMessage.id.desc())
            .first()
        )
        last_id = int(last_msg.id) if last_msg else None

        group_read = get_or_create_group_read(gid, my_id)
        group_read.last_read_message_id = last_id
        group_read.updated_at = datetime.utcnow()

        db.session.add(group_read)
        db.session.commit()


@socketio.on("typing")
def on_typing(data):
    target_id = data.get("target_id")
    sender_id = session.get("user_id")
    sender_name = session.get("username")
    conversation_type = (data.get("conversation_type") or "user").strip().lower()

    if not sender_id or not target_id:
        return

    try:
        target_id = int(target_id)
        sender_id = int(sender_id)
    except Exception:
        return

    if conversation_type == "user":
        emit(
            "typing",
            {
                "sender_id": sender_id,
                "sender_name": sender_name,
                "conversation_type": "user",
                "target_id": target_id,
            },
            room=str(target_id),
        )
        return

    if conversation_type == "group" and user_in_group(sender_id, target_id):
        for member_id in get_group_members(target_id):
            if int(member_id) == int(sender_id):
                continue
            emit(
                "typing",
                {
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "conversation_type": "group",
                    "target_id": target_id,
                },
                room=str(member_id),
            )


@socketio.on("stop_typing")
def on_stop_typing(data):
    target_id = data.get("target_id")
    sender_id = session.get("user_id")
    conversation_type = (data.get("conversation_type") or "user").strip().lower()

    if not sender_id or not target_id:
        return

    try:
        target_id = int(target_id)
        sender_id = int(sender_id)
    except Exception:
        return

    if conversation_type == "user":
        emit(
            "stop_typing",
            {
                "sender_id": sender_id,
                "conversation_type": "user",
                "target_id": target_id,
            },
            room=str(target_id),
        )
        return

    if conversation_type == "group" and user_in_group(sender_id, target_id):
        for member_id in get_group_members(target_id):
            if int(member_id) == int(sender_id):
                continue
            emit(
                "stop_typing",
                {
                    "sender_id": sender_id,
                    "conversation_type": "group",
                    "target_id": target_id,
                },
                room=str(member_id),
            )


# ---------------- CHAMADAS 1-1 ----------------
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


# ---------------- CHAMADAS EM GRUPO ----------------
@socketio.on("invite_group_call")
def invite_group_call(data):
    user_id = session.get("user_id")
    if not user_id:
        return

    group_id = data.get("group_id")
    if not group_id:
        return

    try:
        user_id = int(user_id)
        group_id = int(group_id)
    except Exception:
        return

    if not user_in_group(user_id, group_id):
        return

    caller = User.query.get(user_id)
    group = get_group_by_id(group_id)
    if not group:
        return

    caller_name = (
        caller.display_name if caller and caller.display_name
        else caller.username if caller else "Usuário"
    )

    for member_id in get_group_members(group_id):
        if int(member_id) == int(user_id):
            continue
        socketio.emit(
            "group_call_invite",
            {
                "group_id": group_id,
                "group_name": group.name,
                "from": user_id,
                "from_name": caller_name,
            },
            room=str(member_id),
        )


@socketio.on("join_group_call")
def join_group_call(data):
    user_id = session.get("user_id")
    if not user_id:
        return

    group_id = data.get("group_id")
    if not group_id:
        return

    try:
        user_id = int(user_id)
        group_id = int(group_id)
    except Exception:
        return

    if not user_in_group(user_id, group_id):
        return

    room = group_room_name(group_id)
    join_room(room)

    user = User.query.get(user_id)
    user_name = (
        user.display_name if user and user.display_name
        else user.username if user else "Usuário"
    )

    with group_call_lock:
        existing = list(active_group_calls[group_id])
        active_group_calls[group_id].add(user_id)

    emit(
        "group_call_participants",
        {
            "group_id": group_id,
            "participants": existing,
        }
    )

    socketio.emit(
        "group_call_user_joined",
        {
            "group_id": group_id,
            "user_id": user_id,
            "user_name": user_name,
        },
        room=room,
        include_self=False,
    )


@socketio.on("leave_group_call")
def leave_group_call(data):
    user_id = session.get("user_id")
    if not user_id:
        return

    group_id = data.get("group_id")
    if not group_id:
        return

    try:
        user_id = int(user_id)
        group_id = int(group_id)
    except Exception:
        return

    room = group_room_name(group_id)
    leave_room(room)

    with group_call_lock:
        if group_id in active_group_calls and user_id in active_group_calls[group_id]:
            active_group_calls[group_id].discard(user_id)
            if not active_group_calls[group_id]:
                active_group_calls.pop(group_id, None)

    socketio.emit(
        "group_call_user_left",
        {
            "group_id": group_id,
            "user_id": user_id,
        },
        room=room,
    )


@socketio.on("group_webrtc_offer")
def group_webrtc_offer(data):
    to = data.get("to")
    if not to:
        return
    socketio.emit("group_webrtc_offer", data, room=str(to))


@socketio.on("group_webrtc_answer")
def group_webrtc_answer(data):
    to = data.get("to")
    if not to:
        return
    socketio.emit("group_webrtc_answer", data, room=str(to))


@socketio.on("group_webrtc_ice")
def group_webrtc_ice(data):
    to = data.get("to")
    if not to:
        return
    socketio.emit("group_webrtc_ice", data, room=str(to))


# ---------------- MAIN ----------------
if __name__ == "__main__":
    port = 5000
    socketio.run(app, host="127.0.0.1", port=port)