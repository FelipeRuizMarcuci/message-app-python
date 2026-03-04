# models.py
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)

    display_name = db.Column(db.String(255), nullable=False, default="")
    status_text = db.Column(db.String(255), nullable=True)
    avatar_url = db.Column(db.String(255), nullable=True)

    sent_messages = db.relationship(
        "Message",
        foreign_keys="Message.sender_id",
        backref="sender",
        lazy=True,
        cascade="all, delete-orphan",
    )
    received_messages = db.relationship(
        "Message",
        foreign_keys="Message.receiver_id",
        backref="receiver",
        lazy=True,
        cascade="all, delete-orphan",
    )

class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)

    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    seen = db.Column(db.Boolean, default=False, nullable=False)


# ========================== FUNÇÕES AUXILIARES ==========================

# -------- USUÁRIOS --------
def get_user_by_username(username):
    return User.query.filter_by(username=username).first()

def get_user_by_id(user_id):
    return User.query.get(user_id)

def create_user(username, password_hash):
    user = User(username=username, password=password_hash)
    db.session.add(user)
    db.session.commit()
    return user


# -------- CONVERSAS --------
# def get_conversation_members(conversation_id):
#     members = (
#         db.session.query(User.id, User.username)
#         .join(ConversationMember, ConversationMember.user_id == User.id)
#         .filter(ConversationMember.conversation_id == conversation_id)
#         .all()
#     )
#     return [{"id": m.id, "username": m.username} for m in members]

# def create_conversation(name=None):
#     conv = Conversation(name=name)
#     db.session.add(conv)
#     db.session.commit()
#     return conv.id

# def add_user_to_conversation(conversation_id, user_id):
#     member = ConversationMember(conversation_id=conversation_id, user_id=user_id)
#     db.session.add(member)
#     db.session.commit()


# -------- MENSAGENS --------
def save_message(conversation_id, sender_id, text):
    msg = Message(conversation_id=conversation_id, sender_id=sender_id, text=text)
    db.session.add(msg)
    db.session.commit()

def get_messages(conversation_id, limit=50):
    msgs = (
        Message.query.join(User, Message.sender_id == User.id)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": m.id,
            "conversation_id": m.conversation_id,
            "sender_id": m.sender_id,
            "username": m.sender.username,
            "text": m.text,
            "created_at": m.created_at,
            "seen": m.seen,
        }
        for m in reversed(msgs)
    ]

def count_unread_messages(conversation_id, user_id):
    count = (
        Message.query.filter(
            Message.conversation_id == conversation_id,
            Message.sender_id != user_id,
            Message.seen == False,
        ).count()
    )
    return count

def mark_messages_as_seen(conversation_id, user_id):
    Message.query.filter(
        Message.conversation_id == conversation_id,
        Message.sender_id != user_id,
        Message.seen == False,
    ).update({"seen": True})
    db.session.commit()
