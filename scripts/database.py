# database.py
import json
import os
import uuid
from datetime import datetime

# Usamos un archivo JSON simple para guardar el historial
DB_FILE = "chat_history.json"

class JsonDatabase:
    def __init__(self):
        self.filepath = DB_FILE
        # Estructura en memoria: {"contacts": { "Nombre": { ...datos... } }}
        self.data = {"contacts": {}}
        self.load()

    def load(self):
        """Carga la base de datos del disco."""
        if not os.path.exists(self.filepath):
            self.save() # Crear si no existe
            return

        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content:
                    self.data = {"contacts": {}}
                else:
                    self.data = json.loads(content)
            
            # Asegurar estructura correcta por si el archivo es viejo
            if "contacts" not in self.data:
                self.data = {"contacts": {}}
                
        except Exception as e:
            print(f"Error cargando DB: {e}")
            self.data = {"contacts": {}}

    def save(self):
        """Guarda los datos en disco."""
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error guardando DB: {e}")

    # --- MÉTODOS REQUERIDOS POR LA GUI Y PROTOCOLO ---

    def get_all_contacts(self):
        """Devuelve el diccionario completo de contactos."""
        return self.data.get("contacts", {})

    def get_contact_info(self, cn):
        """Devuelve la info (ip, puerto, estado) de un contacto por su nombre (CN)."""
        return self.data["contacts"].get(cn)

    def add_or_update_contact(self, cn, ip=None, port=None, update_seen=True):
        """Añade un contacto nuevo o actualiza su IP/Puerto."""
        if cn not in self.data["contacts"]:
            self.data["contacts"][cn] = {
                "ip": ip, 
                "port": port, 
                "is_connected": False, 
                "msgs": []
            }
        else:
            # Si ya existe, actualizamos IP/Puerto si nos los pasan
            if ip: self.data["contacts"][cn]["ip"] = ip
            if port: self.data["contacts"][cn]["port"] = port
        
        self.save()

    def rename_contact(self, old_cn, new_cn):
        """Cambia el nombre de un contacto (ej: de 'Ruben_6666' a 'RUBEN SANZ')."""
        if old_cn in self.data["contacts"] and new_cn not in self.data["contacts"]:
            self.data["contacts"][new_cn] = self.data["contacts"][old_cn]
            del self.data["contacts"][old_cn]
            self.save()

    def set_contact_connected(self, cn, is_connected):
        """Marca si estamos conectados (Handshake OK) con alguien."""
        if cn in self.data["contacts"]:
            self.data["contacts"][cn]["is_connected"] = is_connected
            self.save()

    def get_history(self, cn):
        """Devuelve la lista de mensajes de un contacto."""
        return self.data["contacts"].get(cn, {}).get("msgs", [])

    def add_message(self, cn, sender, text, status='pending', timestamp=None):
        """Añade un mensaje al historial y devuelve su ID único."""
        if cn not in self.data["contacts"]:
            self.add_or_update_contact(cn)
        
        if not timestamp:
            timestamp = datetime.now().strftime("%H:%M")
            
        msg_id = str(uuid.uuid4()) # ID único para gestionar los Ticks
        msg = {
            "id": msg_id,
            "sender": sender,
            "text": text,
            "status": status,
            "time": timestamp
        }
        self.data["contacts"][cn]["msgs"].append(msg)
        self.save()
        return msg_id

    def mark_message_status(self, cn, msg_id, status):
        """Actualiza el estado de un mensaje (sent -> delivered)."""
        if cn not in self.data["contacts"]: return
        for msg in self.data["contacts"][cn]["msgs"]:
            if msg.get("id") == msg_id:
                msg["status"] = status
                self.save()
                return

    def get_pending_messages(self, cn):
        """Devuelve mensajes que no se pudieron enviar."""
        return [(i, m) for i, m in enumerate(self.get_history(cn)) if m["status"] == "pending"]

    # Métodos de compatibilidad (por si acaso)
    def load_history(self): return self.data["contacts"]
    def save_message(self, peer_name, ip_port, sender, text): 
        self.add_message(peer_name, sender, text, status="received" if sender!="yo" else "sent")