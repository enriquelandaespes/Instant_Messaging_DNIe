# database.py
import sqlite3
import os
import base64
from datetime import datetime
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend

class EncryptedDB:
    def __init__(self, dnie_manager, user_cn):
        self.dnie = dnie_manager
        self.user_cn = user_cn
        
        print("🔐 Generando clave de base de datos con DNIe...")
        # CHALLENGE: Firmamos un string fijo específico para este usuario
        # Solo el DNIe original puede generar la misma firma y recuperar la clave.
        challenge = f"DB_ACCESS_KEY_FOR_{user_cn}".encode()
        signature = self.dnie.sign_data(challenge)
        
        # Derivar clave AES (Fernet) desde la firma
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b'dnie-secure-chat-salt',
            info=b'db-encryption',
            backend=default_backend()
        )
        key = base64.urlsafe_b64encode(hkdf.derive(signature))
        self.cipher = Fernet(key)
        
        # Nombre de archivo único (hasheado para privacidad)
        db_name = f"chat_storage_{abs(hash(user_cn))}.db"
        self.db_path = db_name
        self._init_db()
        print(f"📂 Base de datos cargada: {db_name}")

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # Tabla Contactos (CN es el identificador único)
        c.execute('''CREATE TABLE IF NOT EXISTS contacts
                     (cn TEXT PRIMARY KEY, last_ip TEXT, last_port INTEGER, session_key BLOB)''')
        
        # Tabla Mensajes
        c.execute('''CREATE TABLE IF NOT EXISTS messages
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      contact_cn TEXT, 
                      sender TEXT, 
                      content BLOB, 
                      timestamp TEXT, 
                      status TEXT)''') # status: 'sent', 'pending', 'received'
        conn.commit()
        conn.close()

    def save_contact(self, cn, ip, port, session_key=None):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        enc_key = self.cipher.encrypt(session_key) if session_key else None
        
        # Upsert (Insert or Update)
        c.execute("INSERT OR REPLACE INTO contacts (cn, last_ip, last_port, session_key) VALUES (?, ?, ?, ?)",
                  (cn, ip, port, enc_key))
        conn.commit()
        conn.close()

    def get_contact_session(self, cn):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT session_key FROM contacts WHERE cn=?", (cn,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            try:
                return self.cipher.decrypt(row[0])
            except: pass
        return None

    def add_message(self, contact_cn, sender, text, status, timestamp=None):
        if not timestamp:
            timestamp = datetime.now().strftime("%H:%M")
            
        # Cifrar el contenido del mensaje antes de guardarlo
        encrypted_text = self.cipher.encrypt(text.encode())
        
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO messages (contact_cn, sender, content, timestamp, status) VALUES (?, ?, ?, ?, ?)",
                  (contact_cn, sender, encrypted_text, timestamp, status))
        conn.commit()
        conn.close()

    def get_history(self, contact_cn):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT sender, content, timestamp, status FROM messages WHERE contact_cn = ? ORDER BY id ASC", (contact_cn,))
        rows = c.fetchall()
        conn.close()
        
        msgs = []
        for r in rows:
            try:
                text = self.cipher.decrypt(r[1]).decode()
                msgs.append({
                    "sender": r[0],
                    "text": text,
                    "time": r[2],
                    "status": r[3]
                })
            except Exception:
                msgs.append({"sender": "Sys", "text": "<Error Descifrado>", "time": "", "status": "error"})
        return msgs

    def get_pending_messages(self, contact_cn):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT id, content FROM messages WHERE contact_cn = ? AND status = 'pending'", (contact_cn,))
        rows = c.fetchall()
        conn.close()
        
        pending = []
        for r in rows:
            try:
                text = self.cipher.decrypt(r[1]).decode()
                pending.append({"id": r[0], "text": text})
            except: pass
        return pending

    def mark_messages_sent(self, msg_ids):
        if not msg_ids: return
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        ids_placeholder = ",".join("?" * len(msg_ids))
        c.execute(f"UPDATE messages SET status='sent' WHERE id IN ({ids_placeholder})", msg_ids)
        conn.commit()
        conn.close()