import MySQLdb
from flask_mysqldb import MySQL

mysql = MySQL()

# ---------------- USUÁRIOS ----------------

def get_user_by_username(username):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    return cur.fetchone()

def get_user_by_id(user_id):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    return cur.fetchone()

def create_user(username, password_hash):
    cur = mysql.connection.cursor()
    cur.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, password_hash))
    mysql.connection.commit()

# ---------------- CONVERSAS ----------------

def get_conversation_members(conversation_id):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT u.id, u.username 
        FROM users u
        JOIN conversation_members m ON m.user_id = u.id
        WHERE m.conversation_id = %s
    """, (conversation_id,))
    return cur.fetchall()

def create_conversation(name=None):
    cur = mysql.connection.cursor()
    cur.execute("INSERT INTO conversations (name) VALUES (%s)", (name,))
    mysql.connection.commit()
    return cur.lastrowid

def add_user_to_conversation(conversation_id, user_id):
    cur = mysql.connection.cursor()
    cur.execute("INSERT INTO conversation_members (conversation_id, user_id) VALUES (%s, %s)", (conversation_id, user_id))
    mysql.connection.commit()

# ---------------- MENSAGENS ----------------

def save_message(conversation_id, sender_id, text):
    cur = mysql.connection.cursor()
    cur.execute("""
        INSERT INTO messages (conversation_id, sender_id, text)
        VALUES (%s, %s, %s)
    """, (conversation_id, sender_id, text))
    mysql.connection.commit()

def get_messages(conversation_id, limit=50):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT m.*, u.username 
        FROM messages m
        JOIN users u ON m.sender_id = u.id
        WHERE conversation_id = %s
        ORDER BY created_at DESC
        LIMIT %s
    """, (conversation_id, limit))
    return cur.fetchall()[::-1]  # inverte para mostrar do mais antigo pro mais novo

def count_unread_messages(conversation_id, user_id):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT COUNT(*) AS unread_count
        FROM messages
        WHERE conversation_id = %s 
        AND sender_id != %s 
        AND seen = FALSE
    """, (conversation_id, user_id))
    return cur.fetchone()['unread_count']

def mark_messages_as_seen(conversation_id, user_id):
    cur = mysql.connection.cursor()
    cur.execute("""
        UPDATE messages 
        SET seen = TRUE
        WHERE conversation_id = %s 
        AND sender_id != %s 
        AND seen = FALSE
    """, (conversation_id, user_id))
    mysql.connection.commit()
