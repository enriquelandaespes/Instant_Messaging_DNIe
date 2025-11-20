# database.py
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
        
        try:
            signature = self.dnie.sign_data(self.file_path.encode())
        except Exception as e:
            print(f"❌ Error accediendo al DNIe para cifrado: {e}")
            raise e
        
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b'json-db-salt',
            info=b'dnie-json-db',
            backend=default_backend()
        )
        key = base64.urlsafe_b64encode(hkdf.derive(signature))
        self.cipher = Fernet(key)
        
        # 'contacts' ahora almacena {cn: {ip: "...", port: ..., msgs: [...]}}
        self.data = {"contacts": {}} 
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
            # Asegurarse de que "msgs" existe para todos los contactos cargados
            for cn_data in self.data["contacts"].values():
                if "msgs" not in cn_data:
                    cn_data["msgs"] = []
        except Exception as e:
            print(f"⚠️ Error cargando BD (posible clave incorrecta o fichero corrupto): {e}")

    def save(self):
        try:
            json_str = json.dumps(self.data, indent=4) # Indent para legibilidad en depuración
            encrypted_content = self.cipher.encrypt(json_str.encode('utf-8'))
            
            with open(self.file_path, 'wb') as f:
                f.write(encrypted_content)
        except Exception as e:
            print(f"Error guardando BD: {e}")

    # --- Función principal para gestionar contactos ---
    def add_or_update_contact(self, cn, ip=None, port=None):
        """
        Añade un contacto o actualiza su IP/Puerto/CN.
        Crea el historial de mensajes si es un contacto nuevo.
        """
        if cn not in self.data["contacts"]:
            self.data["contacts"][cn] = {"ip": None, "port": None, "msgs": []}
        
        if ip: self.data["contacts"][cn]["ip"] = ip
        if port: self.data["contacts"][cn]["port"] = port
        
        self.save()
        return cn # Devolvemos el CN del contacto gestionado

    def get_contact_info(self, cn):
        return self.data["contacts"].get(cn)

    def add_message(self, cn, sender, text, status, timestamp=None):
        # Asegurarse de que el contacto existe con un historial de mensajes
        if cn not in self.data["contacts"]:
             self.add_or_update_contact(cn) # Lo creamos si no existe

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

    def get_history(self, cn):
        contact_data = self.data["contacts"].get(cn)
        return contact_data["msgs"] if contact_data and "msgs" in contact_data else []

    def get_pending_messages(self, cn):
        history = self.get_history(cn)
        pending = []
        for i, msg in enumerate(history):
            if msg["status"] == "pending":
                pending.append((i, msg))
        return pending

    def mark_as_sent(self, cn, msg_indices):
        if cn not in self.data["contacts"]: return
        history = self.data["contacts"][cn]["msgs"]
        for i in msg_indices:
            if i < len(history):
                history[i]["status"] = "sent"
        self.save()