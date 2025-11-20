# db.py
import json
import os

class DatabaseManager:
    def __init__(self, filename="db.json"):
        self.filename = filename
        self.data = {"contacts": {}, "messages": {}}
        self._load_data()

    def _load_data(self):
        if os.path.exists(self.filename):
            with open(self.filename, 'r') as f:
                self.data = json.load(f)
        else:
            self._save_data() # Create an empty file if it doesn't exist

    def _save_data(self):
        with open(self.filename, 'w') as f:
            json.dump(self.data, f, indent=4)

    # --- NUEVA FUNCIÓN ---
    def add_or_update_contact(self, cn, ip=None, port=None):
        """
        Añade un contacto por su CN (nombre del certificado) o actualiza su IP/Puerto.
        Si el contacto ya existe, actualiza sus datos. Si no, lo crea.
        Devuelve el CN del contacto gestionado.
        """
        contact_data = self.data["contacts"].get(cn, {})
        contact_data["cn"] = cn # Asegurarse de que el CN está guardado
        if ip: contact_data["ip"] = ip
        if port: contact_data["port"] = port
        self.data["contacts"][cn] = contact_data
        self._save_data()
        return cn

    def get_contact_info(self, cn):
        return self.data["contacts"].get(cn)

    def update_contact_info(self, cn, ip, port):
        # Esta función puede ser reemplazada por add_or_update_contact
        # pero la mantenemos por compatibilidad si se usa en otros sitios.
        if cn in self.data["contacts"]:
            if ip is not None: self.data["contacts"][cn]["ip"] = ip
            if port is not None: self.data["contacts"][cn]["port"] = port
            self._save_data()

    def add_message(self, cn, sender, text, status, timestamp):
        if cn not in self.data["messages"]:
            self.data["messages"][cn] = []
        
        # Eliminar mensajes si la lista supera un tamaño
        if len(self.data["messages"][cn]) > 1000: # Limite de 1000 mensajes
            self.data["messages"][cn].pop(0)

        self.data["messages"][cn].append({
            "sender": sender,
            "text": text,
            "status": status, # 'pending', 'sent', 'received'
            "time": timestamp
        })
        self._save_data()

    def get_history(self, cn):
        return self.data["messages"].get(cn, [])

    def get_pending_messages(self, cn):
        pending = []
        if cn in self.data["messages"]:
            for i, msg in enumerate(self.data["messages"][cn]):
                if msg["status"] == "pending":
                    pending.append((i, msg))
        return pending

    def mark_as_sent(self, cn, indices):
        if cn in self.data["messages"]:
            for i in indices:
                if i < len(self.data["messages"][cn]):
                    self.data["messages"][cn][i]["status"] = "sent"
            self._save_data()