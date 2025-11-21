# database.py
import json
import os
from datetime import datetime

DB_FILE = "chat_history.json"

class JsonDatabase:
    def __init__(self):
        self.filepath = DB_FILE
        self.ensure_db()

    def ensure_db(self):
        if not os.path.exists(self.filepath):
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump({}, f)

    def load_history(self):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}

    def save_message(self, peer_name, ip_port, sender, text):
        """
        Guarda un mensaje en el JSON.
        peer_name: Nombre del contacto
        ip_port: Clave única (ej: '192.168.1.50:6666')
        sender: 'yo' o el nombre del otro
        """
        data = self.load_history()
        
        # Usamos la IP:PORT como clave primaria para evitar mezclar chats de gente con mismo nombre
        key = ip_port
        
        if key not in data:
            data[key] = {
                "name": peer_name,
                "history": []
            }
        
        # Actualizamos nombre por si cambió
        data[key]["name"] = peer_name
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg_entry = {
            "ts": timestamp,
            "sender": sender,
            "text": text
        }
        
        data[key]["history"].append(msg_entry)
        
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)