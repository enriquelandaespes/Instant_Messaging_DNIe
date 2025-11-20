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
        
        # 'contacts' ahora almacena {cn: {ip: "...", port: ..., is_connected: False, msgs: [...]}}
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
            
            # Post-procesamiento para asegurar la consistencia del esquema
            for cn_data in self.data["contacts"].values():
                cn_data.setdefault("msgs", [])
                cn_data.setdefault("ip", None)
                cn_data.setdefault("port", None)
                cn_data.setdefault("is_connected", False) # Resetear estado de conexión al inicio
                cn_data.setdefault("last_seen", None) # Timestamp de última vez visto
        except Exception as e:
            print(f"⚠️ Error cargando BD (posible clave incorrecta o fichero corrupto): {e}")

    def save(self):
        try:
            json_str = json.dumps(self.data, indent=4) 
            encrypted_content = self.cipher.encrypt(json_str.encode('utf-8'))
            
            with open(self.file_path, 'wb') as f:
                f.write(encrypted_content)
        except Exception as e:
            print(f"Error guardando BD: {e}")

    def add_or_update_contact(self, cn, ip=None, port=None, update_seen=True):
        """
        Añade un contacto o actualiza su IP/Puerto/CN.
        Crea el historial de mensajes si es un contacto nuevo.
        """
        if cn not in self.data["contacts"]:
            self.data["contacts"][cn] = {
                "ip": None, 
                "port": None, 
                "is_connected": False, # Por defecto, no conectado
                "last_seen": None,
                "msgs": []
            }
        
        contact = self.data["contacts"][cn]
        if ip: contact["ip"] = ip
        if port: contact["port"] = port
        if update_seen: contact["last_seen"] = datetime.now().isoformat() # ISO para fácil serialización
        
        self.save()
        return cn 

    def get_contact_info(self, cn):
        """Devuelve toda la información de un contacto."""
        return self.data["contacts"].get(cn)

    def get_all_contacts(self):
        """Devuelve un diccionario de todos los contactos."""
        return self.data["contacts"]
    
    def get_contacts_for_discovery(self):
        """Devuelve una lista de (cn, ip, port) de contactos con IP/Port conocidos."""
        peers = []
        for cn, data in self.data["contacts"].items():
            if data.get("ip") and data.get("port"):
                peers.append((cn, data["ip"], data["port"]))
        return peers

    def set_contact_connected(self, cn, is_connected: bool):
        """Actualiza el estado de conexión de un contacto."""
        if cn in self.data["contacts"]:
            self.data["contacts"][cn]["is_connected"] = is_connected
            if not is_connected: # Si se desconecta, invalidar IP/Puerto para forzar redescubrimiento
                self.data["contacts"][cn]["ip"] = None
                self.data["contacts"][cn]["port"] = None
            self.save()

    def add_message(self, cn, sender, text, status='pending', timestamp=None):
        """
        Añade un mensaje al historial de un contacto.
        status: 'pending', 'sent', 'received', 'error', 'system'
        """
        if cn not in self.data["contacts"]:
             # Si no existe, lo crea. Esto podría pasar si recibimos un msg de un peer no visto.
             self.add_or_update_contact(cn) 

        if not timestamp:
            timestamp = datetime.now().strftime("%H:%M")
            
        msg = {
            "sender": sender,
            "text": text,
            "status": status, 
            "time": timestamp
        }
        self.data["contacts"][cn]["msgs"].append(msg)
        self.save()

    def get_history(self, cn):
        contact_data = self.data["contacts"].get(cn)
        return contact_data["msgs"] if contact_data else []

    def get_pending_messages(self, cn):
        """Devuelve una lista de mensajes con estado 'pending' para un contacto."""
        history = self.get_history(cn)
        pending = []
        for i, msg in enumerate(history):
            if msg["status"] == "pending":
                pending.append((i, msg))
        return pending

    def mark_message_status(self, cn, msg_indices: list, new_status: str):
        """Actualiza el estado de mensajes específicos por su índice."""
        if cn not in self.data["contacts"]: return
        history = self.data["contacts"][cn]["msgs"]
        for i in msg_indices:
            if i < len(history):
                history[i]["status"] = new_status
        self.save()