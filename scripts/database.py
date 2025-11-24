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
                self.data["contacts"] = {}
            
            # Limpiar duplicados automáticamente al cargar
            self._clean_duplicates()
                
        except Exception as e:
            print(f"Error al cargar DB: {e}")
            self.data = {"contacts": {}}

    def save(self):
        # Guarda la base de datos en el archivo JSON
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error al guardar DB: {e}")

    def get_all_contacts(self):
        # Devuelve todos los contactos
        return self.data.get("contacts", {})

    def get_contact_info(self, cn):
        # Devuelve información de un contacto específico
        return self.data["contacts"].get(cn)

    def add_or_update_contact(self, cn, **kwargs):
        # Añade o actualiza un contacto
        if cn not in self.data["contacts"]:
            self.data["contacts"][cn] = {
                "name": kwargs.get("name", cn),
                "ip": kwargs.get("ip"),
                "port": kwargs.get("port"),
                "msgs": [],
                "is_connected": False,
                "last_seen": None,
                "session_key": None,
                "peer_cert": None
            }
        else:
            # Actualizar campos existentes
            for key, value in kwargs.items():
                if key in ["name", "ip", "port"]:
                    self.data["contacts"][cn][key] = value
        
        self.save()

    def update_contact_name(self, contact_id, new_name):
        # Actualiza el nombre de un contacto
        if contact_id in self.data["contacts"]:
            self.data["contacts"][contact_id]["name"] = new_name
            self.save()

    def merge_contacts(self, old_cn, new_cn):
        # Fusiona dos contactos (cuando cambia el ID)
        if old_cn in self.data["contacts"] and new_cn in self.data["contacts"]:
            # Mover mensajes del viejo al nuevo
            old_msgs = self.data["contacts"][old_cn].get("msgs", [])
            self.data["contacts"][new_cn]["msgs"].extend(old_msgs)
            # Eliminar contacto viejo
            del self.data["contacts"][old_cn]
            self.save()

    def set_contact_connected(self, cn, connected):
        # Marca un contacto como conectado/desconectado
        if cn in self.data["contacts"]:
            self.data["contacts"][cn]["is_connected"] = connected
            if not connected:
                self.data["contacts"][cn]["last_seen"] = datetime.now().isoformat()
            self.save()

    def add_message(self, cn, sender, text, status="received", timestamp=None, msg_id=None):
        if cn not in self.data["contacts"]:
            self.add_or_update_contact(cn)
        if msg_id:
            for existing_msg in self.data["contacts"][cn]["msgs"]:
                if existing_msg.get("id") == msg_id:
                    # Mensaje duplicado detectado - NO insertar
                    return msg_id
        else:
            # Generar nuevo ID si no viene proporcionado (mensajes enviados por ti)
            msg_id = str(uuid.uuid4())
    
        msg = {
        "id": msg_id,
        "sender": sender,
        "text": text,
        "timestamp": timestamp or datetime.now().isoformat(),
        "status": status,  # "sent", "delivered", "received", "pending", "error", "system"
        "read": False,
        "sent_timestamp": datetime.now().timestamp() if status == "sent" else None
        }
    
        self.data["contacts"][cn]["msgs"].append(msg)
        self.save()
        return msg_id


    def get_history(self, cn):
        # Devuelve el historial de mensajes con un contacto
        if cn not in self.data["contacts"]:
            return []
        return self.data["contacts"][cn].get("msgs", [])

    def mark_message_status(self, cn, msg_id, status):
        # Marca el estado de un mensaje
        if cn not in self.data["contacts"]:
            return
        
        for msg in self.data["contacts"][cn]["msgs"]:
            if msg.get("id") == msg_id:
                msg["status"] = status
                if status == "sent":
                    msg["sent_timestamp"] = datetime.now().timestamp()
                elif status == "delivered":
                    msg["sent_timestamp"] = None
                self.save()
                return

    def get_pending_messages(self, cn):
        # Devuelve mensajes que no se pudieron enviar.
        return [m for m in self.get_history(cn) if m["status"] == "pending"]
    
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

    def get_session_key(self, cn):
        # Devuelve la clave de sesión guardada (como bytes) o None
        if cn not in self.data["contacts"]: 
            return None
        key_hex = self.data["contacts"][cn].get("session_key")
        if key_hex:
            return bytes.fromhex(key_hex)
        return None
    
    def get_peer_cert(self, cn):
        # Devuelve el certificado del peer guardado (como bytes) o None
        if cn not in self.data["contacts"]: 
            return None
        cert_hex = self.data["contacts"][cn].get("peer_cert")
        if cert_hex:
            return bytes.fromhex(cert_hex)
        return None

    def _clean_duplicates(self):
        """Elimina contactos duplicados que tengan el mismo nombre"""
        contacts_to_remove = []
        contacts_by_name = {}  # {name: [cn1, cn2, ...]}
        
        # Agrupar contactos por nombre
        for cn, info in list(self.data["contacts"].items()):
            name = info.get("name")
            if name:
                if name not in contacts_by_name:
                    contacts_by_name[name] = []
                contacts_by_name[name].append(cn)
        
        # Para cada nombre que tenga duplicados
        for name, contact_ids in contacts_by_name.items():
            if len(contact_ids) > 1:
                # Encontrar el que tiene más mensajes
                best_cn = None
                max_msgs = -1
                
                for cn in contact_ids:
                    msgs_count = len(self.data["contacts"][cn].get("msgs", []))
                    if msgs_count > max_msgs:
                        max_msgs = msgs_count
                        best_cn = cn
                
                # Si todos tienen 0 mensajes, mantener el que sea IP:Puerto
                if max_msgs == 0:
                    for cn in contact_ids:
                        if ":" in cn:  # Es formato IP:Puerto
                            best_cn = cn
                            break
                
                # Eliminar los demás
                for cn in contact_ids:
                    if cn != best_cn:
                        contacts_to_remove.append(cn)
                        print(f"Eliminando contacto duplicado: {cn} (manteniendo {best_cn})")
        
        # Eliminar duplicados
        for cn in contacts_to_remove:
            if cn in self.data["contacts"]:
                del self.data["contacts"][cn]
        
        if contacts_to_remove:
            self.save()

    # Métodos de compatibilidad (por si acaso)
    def load_history(self): return self.data["contacts"]
    def save_message(self, peer_name, ip_port, sender, text): 
        self.add_message(peer_name, sender, text, status="received" if sender!="yo" else "sent")