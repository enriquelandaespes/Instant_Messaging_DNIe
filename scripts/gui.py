# gui.py
import asyncio
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

class ChatGUI:
    # --- FIX: Aceptamos 'db' para que no falle main.py ---
    def __init__(self, protocol, my_nick, db=None):
        self.protocol = protocol
        self.my_nick = my_nick
        self.db = db 
        
        # Datos de contactos
        self.contacts = {} 
        self.selected_idx = 0
        self.current_key = None 

        # Widgets
        self.w_contacts = TextArea(focusable=False, width=35)
        self.w_chat = TextArea(focusable=False, scrollbar=True, wrap_lines=True)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # Layout
        self.layout = Layout(HSplit([
            VSplit([
                Frame(self.w_contacts, title="Contactos"), 
                Frame(self.w_chat, title="Chat Seguro")
            ]),
            Frame(self.w_input, title=f"Escribe (Enter) - Soy: {my_nick}")
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

        self.app = Application(layout=self.layout, key_bindings=kb, full_screen=True, mouse_support=True, refresh_interval=0.5)

    def move_selection(self, delta):
        keys = list(self.contacts.keys())
        if not keys: return
        self.selected_idx = (self.selected_idx + delta) % len(keys)
        self.current_key = keys[self.selected_idx]
        self.refresh_ui()

    def add_peer(self, name, ip, port):
        # FIX DUPLICADOS: Usamos ip:port como clave única
        key = f"{ip}:{port}"
        
        if key in self.contacts:
            # Si ya existe, actualizamos el nombre solo si el nuevo es mejor (no es "Desconocido")
            current_name = self.contacts[key]["name"]
            if "DNIe" in current_name and "DNIe" not in name:
                 self.contacts[key]["name"] = name
            return 

        # Nuevo contacto
        self.contacts[key] = {
            "name": name, 
            "msgs": [], 
            "connected": False,
            "msg_queue": [] 
        }
        if self.current_key is None: 
            self.current_key = key
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, nombre):
        key = f"{addr[0]}:{addr[1]}"
        
        # Registrar contacto si es nuevo
        if key not in self.contacts:
            self.add_peer(nombre, addr[0], addr[1])
        
        contact = self.contacts[key]
        
        if text == "HANDSHAKE_OK":
            contact["connected"] = True
            contact["name"] = nombre 
            contact["msgs"].append({"text": f"🔒 CONEXIÓN SEGURA: {nombre}", "sender": "sys", "status": "info"})
            
            # VACIAR COLA (Enviar mensajes pendientes)
            if contact["msg_queue"]:
                count = len(contact["msg_queue"])
                contact["msgs"].append({"text": f"📤 Enviando {count} mensajes en cola...", "sender": "sys", "status": "info"})
                for msg_text in contact["msg_queue"]:
                    # Reintentar envío
                    exito = self.protocol.enviar_mensaje(addr[0], addr[1], msg_text)
                    # Actualizamos estado en el chat
                    status = "sent" if exito else "pending"
                    contact["msgs"].append({"text": msg_text, "sender": "yo", "status": status})
                contact["msg_queue"] = [] 
                
        else:
            # Mensaje recibido
            contact["connected"] = True
            contact["msgs"].append({"text": text, "sender": "el", "status": "received"})
            # Si tenemos DB, podríamos guardar aquí: if self.db: self.db.save(...)

        self.refresh_ui()

    def handle_enter(self):
        if not self.current_key: return
        text = self.w_input.text.strip()
        self.w_input.text = "" 

        if not text: return

        contact = self.contacts[self.current_key]
        ip, port = self.current_key.split(":")
        port = int(port)

        # Si estamos conectados, intentamos enviar
        if contact["connected"]:
            exito = self.protocol.enviar_mensaje(ip, port, text)
            if exito:
                contact["msgs"].append({"text": text, "sender": "yo", "status": "sent"})
            else:
                # Falló el socket: desconectamos y encolamos
                contact["connected"] = False
                contact["msg_queue"].append(text)
                contact["msgs"].append({"text": text, "sender": "yo", "status": "pending"})
                self.protocol.enviar_handshake(ip, port)
        else:
            # No conectados: Encolamos directamente
            contact["msg_queue"].append(text)
            contact["msgs"].append({"text": text, "sender": "yo", "status": "pending"})
            
            # Iniciamos handshake si no está en curso
            addr_tuple = (ip, port)
            if addr_tuple not in self.protocol.handshake_in_progress:
                contact["msgs"].append({"text": "🟡 Conectando...", "sender": "sys", "status": "info"})
                self.protocol.enviar_handshake(ip, port)

        self.refresh_ui()

    def refresh_ui(self):
        keys = list(self.contacts.keys())
        lines = []
        for i, k in enumerate(keys):
            c = self.contacts[k]
            prefix = "➤ " if k == self.current_key else "  "
            
            # Círculos de estado
            state_icon = "🟢" if c["connected"] else "🔴"
            # Indicador de cola
            queue_info = f"[{len(c['msg_queue'])}]" if c['msg_queue'] else ""
            
            lines.append(f"{prefix}{state_icon} {c['name']} {queue_info}")
            
        self.w_contacts.text = "\n".join(lines)

        if self.current_key:
            c = self.contacts[self.current_key]
            chat_lines = []
            for m in c["msgs"]:
                if m["sender"] == "sys":
                    chat_lines.append(f"--- {m['text']} ---")
                elif m["sender"] == "yo":
                    # Tics de estado
                    tic = "✓✓" if m["status"] == "sent" else "🕒"
                    chat_lines.append(f"Yo: {m['text']} {tic}")
                else:
                    chat_lines.append(f"{c['name']}: {m['text']}")
            
            self.w_chat.text = "\n".join(chat_lines)
            status_txt = "ONLINE" if c["connected"] else "OFFLINE"
            self.w_chat.title = f"Chat con {c['name']} [{status_txt}]"
        else:
            self.w_chat.text = "Esperando contactos..."
            
        self.app.invalidate() 

    async def run(self):
        await self.app.run_async()