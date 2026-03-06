from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


# ========================== MODELS ==========================

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(255), nullable=False)
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

    created_groups = db.relationship(
        "Group",
        foreign_keys="Group.created_by",
        backref="creator",
        lazy=True,
        cascade="all, delete-orphan",
    )

    group_memberships = db.relationship(
        "GroupMember",
        foreign_keys="GroupMember.user_id",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
    )

    sent_group_messages = db.relationship(
        "GroupMessage",
        foreign_keys="GroupMessage.sender_id",
        backref="sender_user",
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


class Group(db.Model):
    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    avatar_url = db.Column(db.String(255), nullable=True)
    description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    members = db.relationship(
        "GroupMember",
        foreign_keys="GroupMember.group_id",
        backref="group",
        lazy=True,
        cascade="all, delete-orphan",
    )

    messages = db.relationship(
        "GroupMessage",
        foreign_keys="GroupMessage.group_id",
        backref="group_ref",
        lazy=True,
        cascade="all, delete-orphan",
    )


class GroupMember(db.Model):
    __tablename__ = "group_members"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    role = db.Column(db.String(20), default="member", nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class GroupMessage(db.Model):
    __tablename__ = "group_messages"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    text = db.Column(db.Text, nullable=False)
    file_url = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class GroupRead(db.Model):
    __tablename__ = "group_reads"

    id = db.Column(db.Integer, primary_key=True)

    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    last_read_message_id = db.Column(
        db.Integer,
        db.ForeignKey("group_messages.id"),
        nullable=True,
    )

    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        db.UniqueConstraint("group_id", "user_id", name="uq_group_reads_group_user"),
    )

    group = db.relationship(
        "Group",
        backref=db.backref("reads", lazy=True, cascade="all, delete-orphan"),
    )

    user = db.relationship(
        "User",
        backref=db.backref("group_reads", lazy=True, cascade="all, delete-orphan"),
    )

    last_read_message = db.relationship(
        "GroupMessage",
        foreign_keys=[last_read_message_id],
    )

# ========================== FUNÇÕES AUXILIARES ==========================

# -------- USUÁRIOS --------
def get_user_by_username(username):
    return User.query.filter_by(username=username).first()


def get_user_by_email(email):
    return User.query.filter_by(email=email).first()


def get_user_by_id(user_id):
    return User.query.get(user_id)


def create_user(
    username,
    email,
    password_hash,
    display_name=None,
    status_text="",
    avatar_url=None,
):
    user = User(
        username=username,
        email=email,
        password=password_hash,
        display_name=display_name or username,
        status_text=status_text,
        avatar_url=avatar_url,
    )
    db.session.add(user)
    db.session.commit()
    return user


# -------- MENSAGENS PRIVADAS --------
def save_private_message(sender_id, receiver_id, text, seen=False):
    msg = Message(
        sender_id=sender_id,
        receiver_id=receiver_id,
        text=text,
        seen=seen,
    )
    db.session.add(msg)
    db.session.commit()
    return msg


def get_private_messages(user1_id, user2_id, limit=50):
    msgs = (
        Message.query.filter(
            db.or_(
                db.and_(Message.sender_id == user1_id, Message.receiver_id == user2_id),
                db.and_(Message.sender_id == user2_id, Message.receiver_id == user1_id),
            )
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "id": m.id,
            "sender_id": m.sender_id,
            "receiver_id": m.receiver_id,
            "username": m.sender.username if m.sender else "",
            "text": m.text,
            "created_at": m.created_at,
            "seen": m.seen,
        }
        for m in reversed(msgs)
    ]


def count_unread_private_messages(receiver_id, sender_id=None):
    query = Message.query.filter(
        Message.receiver_id == receiver_id,
        Message.seen == False,
    )

    if sender_id is not None:
        query = query.filter(Message.sender_id == sender_id)

    return query.count()


def mark_private_messages_as_seen(receiver_id, sender_id=None):
    query = Message.query.filter(
        Message.receiver_id == receiver_id,
        Message.seen == False,
    )

    if sender_id is not None:
        query = query.filter(Message.sender_id == sender_id)

    query.update({"seen": True})
    db.session.commit()


# -------- GRUPOS --------
def create_group(name, created_by, avatar_url=None, description=None):
    group = Group(
        name=name,
        created_by=created_by,
        avatar_url=avatar_url,
        description=description,
    )
    db.session.add(group)
    db.session.commit()

    creator_membership = GroupMember(
        group_id=group.id,
        user_id=created_by,
        role="admin",
    )
    db.session.add(creator_membership)
    db.session.commit()

    return group


def get_group_by_id(group_id):
    return Group.query.get(group_id)


def add_user_to_group(group_id, user_id, role="member"):
    existing = GroupMember.query.filter_by(group_id=group_id, user_id=user_id).first()
    if existing:
        return existing

    member = GroupMember(
        group_id=group_id,
        user_id=user_id,
        role=role,
    )
    db.session.add(member)
    db.session.commit()
    return member


def remove_user_from_group(group_id, user_id):
    member = GroupMember.query.filter_by(group_id=group_id, user_id=user_id).first()
    if member:
        db.session.delete(member)
        db.session.commit()
        return True
    return False


def get_group_members(group_id):
    members = (
        db.session.query(User.id, User.username, User.display_name, User.avatar_url, GroupMember.role)
        .join(GroupMember, GroupMember.user_id == User.id)
        .filter(GroupMember.group_id == group_id)
        .all()
    )

    return [
        {
            "id": m.id,
            "username": m.username,
            "display_name": m.display_name,
            "avatar_url": m.avatar_url,
            "role": m.role,
        }
        for m in members
    ]


def get_user_groups(user_id):
    groups = (
        db.session.query(Group)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(GroupMember.user_id == user_id)
        .order_by(Group.created_at.desc())
        .all()
    )
    return groups


def is_user_in_group(user_id, group_id):
    return (
        GroupMember.query.filter_by(user_id=user_id, group_id=group_id).first()
        is not None
    )


# -------- MENSAGENS DE GRUPO --------
def save_group_message(group_id, sender_id, text, file_url=None):
    msg = GroupMessage(
        group_id=group_id,
        sender_id=sender_id,
        text=text,
        file_url=file_url,
    )
    db.session.add(msg)
    db.session.commit()
    return msg


def get_group_messages(group_id, limit=50):
    msgs = (
        GroupMessage.query.filter_by(group_id=group_id)
        .order_by(GroupMessage.created_at.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "id": m.id,
            "group_id": m.group_id,
            "sender_id": m.sender_id,
            "username": m.sender_user.username if m.sender_user else "",
            "display_name": m.sender_user.display_name if m.sender_user else "",
            "text": m.text,
            "file_url": m.file_url,
            "created_at": m.created_at,
        }
        for m in reversed(msgs)
    ]