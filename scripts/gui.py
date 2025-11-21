# gui.py
import asyncio
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.key_binding import KeyBindings

class ChatGUI:
    def __init__(self, protocol, my_nick, db):
        self.protocol = protocol
        self.my_nick = my_nick
        self.db = db  # Base de datos obligatoria
        
        # Datos
        self.contacts = {} 
        self.selected_idx = 0
        self.current_key = None 
        
        # Cargar historial previo si existe
        self.cargar_historial_inicial()

        # Widgets
        self.w_contacts = TextArea(focusable=False, width=40)
        self.w_chat = TextArea(focusable=False, scrollbar=True, wrap_lines=True)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # Layout
        self.layout = Layout(HSplit([
            VSplit([
                Frame(self.w_contacts, title="Contactos (DNIe)"), 
                Frame(self.w_chat, title="Chat Seguro")
            ]),
            Frame(self.w_input, title=f"Escribe (Enter) | Conectar ('c') - Soy: {my_nick}")
        ]))

        # Keybindings
        kb = KeyBindings()
        
        @kb.add("c-c")
        def _(event): event.app.exit()

        @kb.add("up")
        def _(event): self.move_selection(-1)
        
        @kb.add("down")
        def _(event): self.move_selection(1)

        @kb.add("enter")
        def _(event): self.handle_enter()
        
        # --- NUEVO: Tecla 'c' para conectar manualmente ---
        @kb.add("c")
        def _(event): self.conectar_manual()

        self.app = Application(layout=self.layout, key_bindings=kb, full_screen=True, mouse_support=True, refresh_interval=0.5)

    def cargar_historial_inicial(self):
        """ Carga mensajes antiguos del JSON al iniciar """
        historial = self.db.load_history()
        for key, data in historial.items():
            # Reconstruimos la estructura de contactos
            name = data.get("name", "Desconocido")
            msgs = []
            for m in data.get("history", []):
                # Convertimos formato DB a formato GUI
                status = "sent" if m["sender"] == "yo" else "received"
                msgs.append({"text": m["text"], "sender": m["sender"], "status": status})
            
            self.contacts[key] = {
                "name": name,
                "msgs": msgs,
                "connected": False, # Empiezan desconectados
                "msg_queue": []
            }

    def move_selection(self, delta):
        keys = list(self.contacts.keys())
        if not keys: return
        self.selected_idx = (self.selected_idx + delta) % len(keys)
        self.current_key = keys[self.selected_idx]
        self.refresh_ui()

    def add_peer(self, name, ip, port):
        key = f"{ip}:{port}"
        
        # Si ya existe, solo actualizamos IP o nombre, PERO NO CONECTAMOS
        if key in self.contacts:
            if "DNIe" in self.contacts[key]["name"] and "DNIe" not in name:
                 self.contacts[key]["name"] = name
            return 

        # Nuevo contacto (Sin conectar aún)
        self.contacts[key] = {
            "name": name, 
            "msgs": [], 
            "connected": False,
            "msg_queue": [] 
        }
        if self.current_key is None: 
            self.current_key = key
        
        self.refresh_ui()

    def conectar_manual(self):
        """ Se llama al pulsar 'c' """
        if not self.current_key: return
        
        contact = self.contacts[self.current_key]
        ip, port = self.current_key.split(":")
        port = int(port)
        
        contact["msgs"].append({"text": "🟡 Iniciando Handshake manual...", "sender": "sys", "status": "info"})
        self.protocol.enviar_handshake(ip, port)
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, nombre):
        key = f"{addr[0]}:{addr[1]}"
        
        if key not in self.contacts:
            self.add_peer(nombre, addr[0], addr[1])
        
        contact = self.contacts[key]
        
        if text == "HANDSHAKE_OK":
            contact["connected"] = True
            contact["name"] = nombre 
            contact["msgs"].append({"text": f"🔒 CONEXIÓN ESTABLECIDA: {nombre}", "sender": "sys", "status": "info"})
            
            # Enviar cola pendiente
            if contact["msg_queue"]:
                for msg_text in contact["msg_queue"]:
                    self.protocol.enviar_mensaje(addr[0], addr[1], msg_text)
                    # Guardar en DB
                    self.db.save_message(contact["name"], key, "yo", msg_text)
                    contact["msgs"].append({"text": msg_text, "sender": "yo", "status": "sent"})
                contact["msg_queue"] = [] 
                
        else:
            # Mensaje recibido
            contact["connected"] = True
            contact["msgs"].append({"text": text, "sender": nombre, "status": "received"})
            # GUARDAR EN DB
            self.db.save_message(nombre, key, nombre, text)

        self.refresh_ui()

    def handle_enter(self):
        if not self.current_key: return
        text = self.w_input.text.strip()
        self.w_input.text = "" 
        if not text: return

        contact = self.contacts[self.current_key]
        ip, port = self.current_key.split(":")
        port = int(port)

        # Intentamos enviar
        enviado = self.protocol.enviar_mensaje(ip, port, text)

        if enviado:
            contact["connected"] = True
            contact["msgs"].append({"text": text, "sender": "yo", "status": "sent"})
            # GUARDAR EN DB
            self.db.save_message(contact["name"], self.current_key, "yo", text)
        else:
            # Falló -> Desconectado -> Cola
            contact["connected"] = False
            contact["msg_queue"].append(text)
            contact["msgs"].append({"text": text, "sender": "yo", "status": "pending"})
            
        self.refresh_ui()

    def refresh_ui(self):
        keys = list(self.contacts.keys())
        lines = []
        for i, k in enumerate(keys):
            c = self.contacts[k]
            prefix = "➤ " if k == self.current_key else "  "
            state_icon = "🟢" if c["connected"] else "🔴"
            queue_info = f"[{len(c['msg_queue'])}]" if c['msg_queue'] else ""
            lines.append(f"{prefix}{state_icon} {c['name']} {queue_info}")
            
        self.w_contacts.text = "\n".join(lines)

        if self.current_key:
            c = self.contacts[self.current_key]
            chat_lines = []
            for m in c["msgs"]:
                sender = m["sender"]
                if sender == "sys":
                    chat_lines.append(f"--- {m['text']} ---")
                elif sender == "yo":
                    tic = "✓✓" if m["status"] == "sent" else "🕒"
                    chat_lines.append(f"Yo: {m['text']} {tic}")
                else:
                    # Mensajes del otro
                    chat_lines.append(f"{sender}: {m['text']}")
            
            self.w_chat.text = "\n".join(chat_lines)
            status_txt = "ONLINE" if c["connected"] else "OFFLINE"
            self.w_chat.title = f"Chat con {c['name']} [{status_txt}]"
        else:
            self.w_chat.text = "Usa flechas para elegir chat. Pulsa 'c' para conectar."
            
        self.app.invalidate() 

    async def run(self):
        await self.app.run_async()