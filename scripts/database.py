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
        # Carga la base de datos si ya existe, si no la crea
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
        # Guarda los datos en el archivo JSON(De momento sin cifrar)
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error guardando DB: {e}")
    # --- MÉTODOS REQUERIDOS POR LA GUI Y PROTOCOLO ---

    def get_all_contacts(self):
        # Devuelve todos los contactos 
        return self.data.get("contacts", {})

    def get_contact_info(self, cn):
        # Devuelve la info (ip, puerto, estado) de un contacto por su nombre (CN).
        return self.data["contacts"].get(cn)

    def add_or_update_contact(self, cn, name=None, ip=None, port=None, session_key=None, update_seen=True):
        # Añade un contacto nuevo o actualiza su IP/Puerto y session_key.
        if cn not in self.data["contacts"]:
            self.data["contacts"][cn] = {
                "name": name or cn,
                "ip": ip, 
                "port": port, 
                "session_key": session_key, # Nueva clave de sesión persistente
                "is_connected": False, 
                "msgs": []
            }
        else:
            # Si ya existe, actualizamos los campos que nos pasan
            if name: self.data["contacts"][cn]["name"] = name
            if ip: self.data["contacts"][cn]["ip"] = ip
            if port: self.data["contacts"][cn]["port"] = port
            if session_key: self.data["contacts"][cn]["session_key"] = session_key
        
        self.save()
    
    def update_contact_name(self, cn, name):
        # Actualiza solo el nombre de un contacto  
        if cn in self.data["contacts"]:
            self.data["contacts"][cn]["name"] = name
            self.save()

    def rename_contact(self, old_cn, new_cn):
        # Cambia el nombre de un contacto
        if old_cn in self.data["contacts"] and new_cn not in self.data["contacts"]:
            self.data["contacts"][new_cn] = self.data["contacts"][old_cn]
            del self.data["contacts"][old_cn]
            self.save()

    def set_contact_connected(self, cn, is_connected):
        # Marca si estamos conectados (Handshake OK) con alguien.
        if cn in self.data["contacts"]:
            self.data["contacts"][cn]["is_connected"] = is_connected
            self.save()

    def add_message(self, cn, sender, text, status='pending', timestamp=None):
        # Añade un mensaje al historial y devuelve su ID único
        if cn not in self.data["contacts"]:
            self.add_or_update_contact(cn)
        
        now = datetime.now()
        if not timestamp:
            timestamp = now.strftime("%H:%M")
            
        msg_id = str(uuid.uuid4()) # ID único para gestionar los Ticks (Visual)
        msg = {
            "id": msg_id,
            "sender": sender,
            "text": text,
            "status": status,
            "time": timestamp,
            "full_date": now.strftime("%Y-%m-%d %H:%M"),  # Fecha completa para comparaciones
            "sent_timestamp": now.timestamp() if status == "sent" else None,
            "read": False  # Inicializar como no leído
        }
        self.data["contacts"][cn]["msgs"].append(msg)
        self.save()
        return msg_id

    def mark_message_status(self, cn, msg_id, status):
        # Actualiza el estado de un mensaje
        if cn not in self.data["contacts"]: return
        for msg in self.data["contacts"][cn]["msgs"]:
            if msg.get("id") == msg_id:
                msg["status"] = status
                self.save()
                return

    def get_pending_messages(self, cn):
        # Devuelve mensajes que no se pudieron enviar.
        return [(i, m) for i, m in enumerate(self.get_history(cn)) if m["status"] == "pending"]
    
    def get_unread_count(self, cn, my_nick):
        # Devuelve el número de mensajes no leídos de un contacto
        if cn not in self.data["contacts"]: return 0
        msgs = self.data["contacts"][cn]["msgs"]
        # Contar mensajes RECIBIDOS (status='received') que no están leídos
        return sum(1 for m in msgs if m.get("status") == "received" and not m.get("read", False))
    
    def mark_messages_as_read(self, cn, my_nick):
        # Marca todos los mensajes de un contacto como leídos(Para el doble tick)
        if cn not in self.data["contacts"]: return
        msgs = self.data["contacts"][cn]["msgs"]
        changed = False
        for m in msgs:
            # Marcar solo mensajes RECIBIDOS (status='received') como leídos
            if m.get("status") == "received" and not m.get("read", False):
                m["read"] = True
                changed = True
        if changed:
            self.save()
    
    def mark_message_as_read_by_id(self, cn, msg_id):
        # Marca un mensaje específico como leído
        if cn not in self.data["contacts"]: return
        for msg in self.data["contacts"][cn]["msgs"]:
            if msg.get("id") == msg_id:
                msg["read"] = True
                self.save()
                return
    
    def check_message_timeouts(self, cn, timeout_seconds=5):
        # Verifica si hay mensajes 'sent' que no recibieron ACK en el tiempo límite. Retorna True si hay timeouts (indica desconexión).
        if cn not in self.data["contacts"]: 
            return False
        
        now = datetime.now().timestamp()
        has_timeout = False
        
        for msg in self.data["contacts"][cn]["msgs"]:
            if msg.get("status") == "sent" and msg.get("sent_timestamp"):
                elapsed = now - msg["sent_timestamp"]
                if elapsed > timeout_seconds:
                    # Timeout detectado - marcar como pending
                    msg["status"] = "pending"
                    msg["sent_timestamp"] = None
                    has_timeout = True
        
        if has_timeout:
            self.save()
        
        return has_timeout

    # Métodos de compatibilidad (por si acaso)
    def load_history(self): return self.data["contacts"]
    def save_message(self, peer_name, ip_port, sender, text): 
        self.add_message(peer_name, sender, text, status="received" if sender!="yo" else "sent")