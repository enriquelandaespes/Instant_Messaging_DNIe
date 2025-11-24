import json
import os
import uuid
from datetime import datetime

DB_FILE = "chat_history.json"

class JsonDatabase:
    def __init__(self):
        self.filepath = DB_FILE
        self.data = {"contacts": {}}
        self.load()

    def load(self):
        if not os.path.exists(self.filepath):
            self.save()
            return

        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content:
                    self.data = {"contacts": {}}
                else:
                    self.data = json.loads(content)
            
            if "contacts" not in self.data:
                self.data["contacts"] = {}
            
            self._clean_duplicates()
                
        except Exception as e:
            print(f"Error al cargar DB: {e}")
            self.data = {"contacts": {}}

    def save(self):
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error al guardar DB: {e}")

    def get_all_contacts(self):
        return self.data.get("contacts", {})

    def get_contact_info(self, cn):
        return self.data["contacts"].get(cn)

    def add_or_update_contact(self, cn, **kwargs):
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
            for key, value in kwargs.items():
                if key in ["name", "ip", "port", "session_key", "peer_cert"]:
                    self.data["contacts"][cn][key] = value
        
        self.save()

    def set_contact_connected(self, cn, connected):
        if cn in self.data["contacts"]:
            self.data["contacts"][cn]["is_connected"] = connected
            if not connected:
                self.data["contacts"][cn]["last_seen"] = datetime.now().isoformat()
            self.save()

    def add_message(self, cn, sender, text, status="received", timestamp=None, msg_id=None):
        """
        Añade un mensaje al historial.
        Si msg_id se proporciona (mensaje recibido), verifica que no exista ya para evitar duplicados.
        """
        if cn not in self.data["contacts"]:
            self.add_or_update_contact(cn)
        
        # ✨ DEDUPLICACIÓN: Si msg_id viene del exterior, verificar si ya existe
        if msg_id:
            for existing_msg in self.data["contacts"][cn]["msgs"]:
                if existing_msg.get("id") == msg_id:
                    # Mensaje duplicado detectado - NO insertar
                    return msg_id
        else:
            # Generar nuevo ID si no viene proporcionado
            msg_id = str(uuid.uuid4())
        
        msg = {
            "id": msg_id,
            "sender": sender,
            "text": text,
            "timestamp": timestamp or datetime.now().isoformat(),
            "status": status,
            "read": False,
            "sent_timestamp": datetime.now().timestamp() if status == "sent" else None
        }
        
        self.data["contacts"][cn]["msgs"].append(msg)
        self.save()
        return msg_id

    def get_history(self, cn):
        if cn not in self.data["contacts"]:
            return []
        return self.data["contacts"][cn].get("msgs", [])

    def mark_message_status(self, cn, msg_id, status):
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
        return [m for m in self.get_history(cn) if m["status"] == "pending"]
    
    def get_unread_count(self, cn, my_nick):
        if cn not in self.data["contacts"]: return 0
        msgs = self.data["contacts"][cn]["msgs"]
        return sum(1 for m in msgs if m.get("status") == "received" and not m.get("read", False))
    
    def mark_messages_as_read(self, cn, my_nick):
        if cn not in self.data["contacts"]: return
        msgs = self.data["contacts"][cn]["msgs"]
        changed = False
        for m in msgs:
            if m.get("status") == "received" and not m.get("read", False):
                m["read"] = True
                changed = True
        if changed:
            self.save()
    
    def mark_message_as_read_by_id(self, cn, msg_id):
        if cn not in self.data["contacts"]: return
        for msg in self.data["contacts"][cn]["msgs"]:
            if msg.get("id") == msg_id:
                msg["read"] = True
                self.save()
                return
    
    def check_message_timeouts(self, cn, timeout_seconds=5):
        if cn not in self.data["contacts"]: 
            return False
        
        now = datetime.now().timestamp()
        has_timeout = False
        
        for msg in self.data["contacts"][cn]["msgs"]:
            if msg.get("status") == "sent" and msg.get("sent_timestamp"):
                elapsed = now - msg["sent_timestamp"]
                if elapsed > timeout_seconds:
                    msg["status"] = "pending"
                    msg["sent_timestamp"] = None
                    has_timeout = True
        
        if has_timeout:
            self.save()
        
        return has_timeout

    def get_session_key(self, cn):
        if cn not in self.data["contacts"]: 
            return None
        key_hex = self.data["contacts"][cn].get("session_key")
        if key_hex:
            return bytes.fromhex(key_hex)
        return None
    
    def get_peer_cert(self, cn):
        if cn not in self.data["contacts"]: 
            return None
        cert_hex = self.data["contacts"][cn].get("peer_cert")
        if cert_hex:
            return bytes.fromhex(cert_hex)
        return None

    def _clean_duplicates(self):
        contacts_to_remove = []
        contacts_by_name = {}
        
        for cn, info in list(self.data["contacts"].items()):
            name = info.get("name")
            if name:
                if name not in contacts_by_name:
                    contacts_by_name[name] = []
                contacts_by_name[name].append(cn)
        
        for name, contact_ids in contacts_by_name.items():
            if len(contact_ids) > 1:
                best_cn = None
                max_msgs = -1
                
                for cn in contact_ids:
                    msgs_count = len(self.data["contacts"][cn].get("msgs", []))
                    if msgs_count > max_msgs:
                        max_msgs = msgs_count
                        best_cn = cn
                
                if max_msgs == 0:
                    for cn in contact_ids:
                        if ":" in cn:
                            best_cn = cn
                            break
                
                for cn in contact_ids:
                    if cn != best_cn:
                        contacts_to_remove.append(cn)
                        print(f"Eliminando contacto duplicado: {cn} (manteniendo {best_cn})")
        
        for cn in contacts_to_remove:
            if cn in self.data["contacts"]:
                del self.data["contacts"][cn]
        
        if contacts_to_remove:
            self.save()
