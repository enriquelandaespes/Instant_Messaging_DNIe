# json_db.py
import json
import os
import base64
from datetime import datetime
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend

class JsonEncryptedDB:
    def __init__(self, dnie_manager, unique_id):
        self.dnie = dnie_manager
        self.file_path = f"dnie_chat_{unique_id}.json"
        
        print(f"🔐 Inicializando BD cifrada: {self.file_path}")
        
        # 1. Derivar clave de cifrado usando una firma del DNIe
        # Firmamos el nombre del archivo para obtener la clave maestra
        signature = self.dnie.sign_data(self.file_path.encode())
        
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b'json-db-salt',
            info=b'dnie-json-db',
            backend=default_backend()
        )
        key = base64.urlsafe_b64encode(hkdf.derive(signature))
        self.cipher = Fernet(key)
        
        # 2. Cargar o Crear BD
        self.data = {
            "contacts": {}  # Estructura: { "CN": { "ip": "...", "msgs": [...] } }
        }
        self.load()

    def load(self):
        if not os.path.exists(self.file_path):
            return
        
        try:
            with open(self.file_path, 'rb') as f:
                encrypted_content = f.read()
            
            if not encrypted_content: return

            decrypted_json = self.cipher.decrypt(encrypted_content).decode('utf-8')
            self.data = json.loads(decrypted_json)
        except Exception as e:
            print(f"⚠️ Error cargando BD (posible clave incorrecta o fichero corrupto): {e}")

    def save(self):
        try:
            json_str = json.dumps(self.data)
            encrypted_content = self.cipher.encrypt(json_str.encode('utf-8'))
            
            with open(self.file_path, 'wb') as f:
                f.write(encrypted_content)
        except Exception as e:
            print(f"Error guardando BD: {e}")

    def add_message(self, cn, sender, text, status, timestamp=None):
        if cn not in self.data["contacts"]:
            self.data["contacts"][cn] = {"msgs": [], "ip": None, "port": None}
        
        if not timestamp:
            timestamp = datetime.now().strftime("%H:%M")
            
        msg = {
            "sender": sender,
            "text": text,
            "status": status, # 'pending', 'sent', 'received'
            "time": timestamp
        }
        self.data["contacts"][cn]["msgs"].append(msg)
        self.save()

    def update_contact_info(self, cn, ip, port):
        if cn not in self.data["contacts"]:
            self.data["contacts"][cn] = {"msgs": [], "ip": ip, "port": port}
        else:
            self.data["contacts"][cn]["ip"] = ip
            self.data["contacts"][cn]["port"] = port
        self.save()

    def get_history(self, cn):
        if cn in self.data["contacts"]:
            return self.data["contacts"][cn]["msgs"]
        return []

    def get_pending_messages(self, cn):
        if cn not in self.data["contacts"]: return []
        # Devolvemos índices y mensajes
        pending = []
        for i, msg in enumerate(self.data["contacts"][cn]["msgs"]):
            if msg["status"] == "pending":
                pending.append((i, msg))
        return pending

    def mark_as_sent(self, cn, msg_indices):
        if cn not in self.data["contacts"]: return
        for i in msg_indices:
            if i < len(self.data["contacts"][cn]["msgs"]):
                self.data["contacts"][cn]["msgs"][i]["status"] = "sent"
        self.save()