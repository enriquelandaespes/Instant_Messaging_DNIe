# gui.py
import asyncio
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

class ChatGUI:
    def __init__(self, protocol, my_nick, db=None):
        self.protocol = protocol
        self.my_nick = my_nick
        self.db = db 
        
        # Estructura contacts: 
        # { "ip:port": { "name": "...", "msgs": [], "connected": False, "msg_queue": [] } }
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
                Frame(self.w_contacts, title="Contactos (DNIe)"), 
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
        """ Se llama cuando el Discovery encuentra a alguien """
        key = f"{ip}:{port}"
        
        # 1. EVITAR DUPLICADOS Y RECONEXIONES INNECESARIAS
        if key in self.contacts:
            # Si ya lo conocemos y estamos conectados, no hacemos nada (para no "volver a conectar")
            if self.contacts[key]["connected"]:
                return
            
            # Si el nombre mejora (ya no es Desconocido), actualizamos
            current_name = self.contacts[key]["name"]
            if "DNIe" in current_name and "DNIe" not in name:
                 self.contacts[key]["name"] = name
            
            # Si no estamos conectados, el Discovery ha "vuelto" a verle -> Handshake Automático
            # (Solo si no hay uno ya en proceso)
            addr_tuple = (ip, port)
            if addr_tuple not in self.protocol.handshake_in_progress:
                self.protocol.enviar_handshake(ip, port)
            return 

        # 2. NUEVO CONTACTO -> HANDSHAKE AUTOMÁTICO
        # Lo registramos
        self.contacts[key] = {
            "name": name, 
            "msgs": [], 
            "connected": False,
            "msg_queue": [] 
        }
        if self.current_key is None: 
            self.current_key = key
            
        # ✨ MAGIA: Lanzamos Handshake en cuanto se detecta ✨
        self.protocol.enviar_handshake(ip, port)
        
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, nombre):
        key = f"{addr[0]}:{addr[1]}"
        
        # Si nos habla alguien que no ha detectado el discovery, lo añadimos
        if key not in self.contacts:
            # Al añadirlo aquí, NO lanzamos handshake manual porque ya nos están hablando ellos
            self.contacts[key] = {
                "name": nombre, "msgs": [], "connected": True, "msg_queue": []
            }
            if self.current_key is None: self.current_key = key
        
        contact = self.contacts[key]
        
        if text == "HANDSHAKE_OK":
            contact["connected"] = True
            contact["name"] = nombre 
            contact["msgs"].append({"text": f"🔒 CONEXIÓN ESTABLECIDA: {nombre}", "sender": "sys", "status": "info"})
            
            # VACIAR COLA PENDIENTE
            if contact["msg_queue"]:
                count = len(contact["msg_queue"])
                contact["msgs"].append({"text": f"📤 Enviando {count} mensajes guardados...", "sender": "sys", "status": "info"})
                for msg_text in contact["msg_queue"]:
                    exito = self.protocol.enviar_mensaje(addr[0], addr[1], msg_text)
                    status = "sent" if exito else "pending"
                    contact["msgs"].append({"text": msg_text, "sender": "yo", "status": status})
                contact["msg_queue"] = [] 
                
        else:
            # Mensaje de texto recibido
            contact["connected"] = True
            contact["msgs"].append({"text": text, "sender": "el", "status": "received"})
            if self.db: 
                # Guardar en BD real si existiera
                pass

        self.refresh_ui()

    def handle_enter(self):
        if not self.current_key: return
        text = self.w_input.text.strip()
        self.w_input.text = "" # Limpiar input
        if not text: return

        contact = self.contacts[self.current_key]
        ip, port = self.current_key.split(":")
        port = int(port)

        # --- LÓGICA FLUIDA DE ENVÍO ---
        # Intentamos enviar directamente. El protocolo nos dirá (True/False) si salió.
        enviado = self.protocol.enviar_mensaje(ip, port, text)

        if enviado:
            # Si salió, marcamos como enviado y conectado
            contact["connected"] = True
            contact["msgs"].append({"text": text, "sender": "yo", "status": "sent"})
        else:
            # Si falló (Socket error o sin sesión), guardamos en "Base de Datos" (Cola)
            # NO forzamos handshake aquí para no molestar si el usuario "vuelve".
            # Se enviará cuando el discovery lo vuelva a ver o nos hablen.
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
                if m["sender"] == "sys":
                    chat_lines.append(f"--- {m['text']} ---")
                elif m["sender"] == "yo":
                    tic = "✓✓" if m["status"] == "sent" else "🕒"
                    chat_lines.append(f"Yo: {m['text']} {tic}")
                else:
                    chat_lines.append(f"{c['name']}: {m['text']}")
            
            self.w_chat.text = "\n".join(chat_lines)
            status_txt = "ONLINE" if c["connected"] else "OFFLINE"
            self.w_chat.title = f"Chat con {c['name']} [{status_txt}]"
        else:
            self.w_chat.text = "Esperando detección automática..."
            
        self.app.invalidate() 

    async def run(self):
        await self.app.run_async()