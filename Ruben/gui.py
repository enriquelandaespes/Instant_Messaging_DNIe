# gui.py
import asyncio
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

class ChatGUI:
    def __init__(self, protocol, my_nick):
        self.protocol = protocol
        self.my_nick = my_nick
        
        # Datos
        self.contacts = {} # { "ip:port": {"name": "Pepe", "msgs": []} }
        self.selected_idx = 0
        self.current_key = None # ip:port seleccionado

        # Widgets
        self.w_contacts = TextArea(focusable=False, width=30)
        self.w_chat = TextArea(focusable=False, scrollbar=True, wrap_lines=True)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # Layout
        self.layout = Layout(HSplit([
            VSplit([
                Frame(self.w_contacts, title="Vecinos (mDNS)"), 
                Frame(self.w_chat, title="Chat Seguro")
            ]),
            Frame(self.w_input, title=f"Escribe (Enter para Enviar/Handshake) - Soy: {my_nick}")
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

        self.app = Application(layout=self.layout, key_bindings=kb, full_screen=True, mouse_support=True)

    def move_selection(self, delta):
        keys = list(self.contacts.keys())
        if not keys: return
        self.selected_idx = (self.selected_idx + delta) % len(keys)
        self.current_key = keys[self.selected_idx]
        self.refresh_ui()

    def add_peer(self, name, ip, port):
        key = f"{ip}:{port}"
        if key not in self.contacts:
            self.contacts[key] = {"name": name, "msgs": [], "connected": False}
            if self.current_key is None: 
                self.current_key = key
            self.refresh_ui()

    def on_protocol_msg(self, addr, text, nombre):
        key = f"{addr[0]}:{addr[1]}"
        
        # Si es nuevo (handshake entrante)
        if key not in self.contacts:
            self.contacts[key] = {"name": nombre, "msgs": [], "connected": True}
        
        contact = self.contacts[key]
        
        if text == "HANDSHAKE_OK":
            contact["connected"] = True
            contact["name"] = nombre # Actualizar con nombre real del DNIe
            contact["msgs"].append(f"🔒 --- CONEXIÓN SEGURA CON {nombre} ---")
        else:
            contact["msgs"].append(f"{nombre}: {text}")
            
        self.refresh_ui()

    def handle_enter(self):
        if not self.current_key: return
        text = self.w_input.text.strip()
        self.w_input.text = "" # Limpiar

        contact = self.contacts[self.current_key]
        ip, port = self.current_key.split(":")
        port = int(port)

        # LÓGICA MÁGICA: SI NO CONECTADO -> HANDSHAKE. SI SI -> MENSAJE
        if not contact["connected"]:
            self.protocol.enviar_handshake(ip, port)
            contact["msgs"].append("🟡 Enviando solicitud de handshake...")
        elif text:
            self.protocol.enviar_mensaje(ip, port, text)
            contact["msgs"].append(f"Yo: {text}")
        
        self.refresh_ui()

    def refresh_ui(self):
        # 1. Lista Contactos
        keys = list(self.contacts.keys())
        lines = []
        for i, k in enumerate(keys):
            c = self.contacts[k]
            prefix = "➤ " if k == self.current_key else "  "
            icon = "🔒" if c["connected"] else "🌐"
            lines.append(f"{prefix}{icon} {c['name']}")
        self.w_contacts.text = "\n".join(lines)

        # 2. Chat
        if self.current_key:
            msgs = self.contacts[self.current_key]["msgs"]
            self.w_chat.text = "\n".join(msgs)
            self.w_chat.title = f"Chat con {self.contacts[self.current_key]['name']}"
        else:
            self.w_chat.text = "Esperando vecinos..."
            
        self.app.invalidate() # Forzar repintado

    async def run(self):
        await self.app.run_async()