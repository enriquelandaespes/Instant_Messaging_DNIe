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
        
        # Datos: { "IP:PORT": { "name": "...", "msgs": [], "connected": False } }
        self.contacts = {}
        self.contact_keys = [] # Lista ordenada de claves para la navegación
        self.selected_idx = 0
        self.current_key = None 

        # Widgets
        self.w_contacts = TextArea(focusable=False, width=30)
        self.w_chat = TextArea(focusable=False, scrollbar=True, wrap_lines=True)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # Diseño (Layout)
        self.layout = Layout(HSplit([
            VSplit([
                Frame(self.w_contacts, title="Vecinos (Flechas + Enter)"), 
                Frame(self.w_chat, title="Chat Seguro")
            ]),
            Frame(self.w_input, title=f"Escribe mensaje (Soy: {my_nick})")
        ]))

        # Teclas
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
        if not self.contact_keys: return
        self.selected_idx = (self.selected_idx + delta) % len(self.contact_keys)
        self.current_key = self.contact_keys[self.selected_idx]
        self.refresh_ui()

    def add_peer(self, name, ip, port):
        key = f"{ip}:{port}"
        # Limpiamos el nombre mDNS (User_6666 -> User)
        display_name = name.split('_')[0]
        
        if key not in self.contacts:
            self.contacts[key] = {"name": display_name, "msgs": [], "connected": False}
            self.contact_keys.append(key)
            
            # Si es el primero, lo seleccionamos automáticamente
            if self.current_key is None: 
                self.current_key = key
                self.selected_idx = 0
            
            self.refresh_ui()

    def on_protocol_msg(self, addr, text, nombre):
        key = f"{addr[0]}:{addr[1]}"
        
        # Si nos habla un desconocido (Handshake entrante), lo añadimos
        if key not in self.contacts:
            self.contacts[key] = {"name": nombre, "msgs": [], "connected": False}
            self.contact_keys.append(key)
            if self.current_key is None: self.current_key = key

        contact = self.contacts[key]
        
        if text == "HANDSHAKE_OK":
            contact["connected"] = True
            contact["name"] = nombre # Actualizar con nombre del certificado
            contact["msgs"].append(f"✅ --- CONEXIÓN SEGURA CON {nombre} ---")
        else:
            # Mensaje normal
            contact["connected"] = True
            contact["msgs"].append(f"{nombre}: {text}")
            
        self.refresh_ui()

    def handle_enter(self):
        if not self.current_key: return
        text = self.w_input.text.strip()
        self.w_input.text = "" # Limpiar input

        contact = self.contacts[self.current_key]
        ip, port = self.current_key.split(":")
        port = int(port)

        # LÓGICA MÁGICA:
        if not contact["connected"]:
            # 1. Si NO hay conexión -> Hacemos Handshake
            self.protocol.enviar_handshake(ip, port)
            contact["msgs"].append("🟡 Solicitando Handshake...")
        elif text:
            # 2. Si SI hay conexión -> Enviamos Mensaje
            self.protocol.enviar_mensaje(ip, port, text)
            contact["msgs"].append(f"Yo: {text}")
        
        self.refresh_ui()

    def refresh_ui(self):
        # 1. Renderizar Lista Contactos
        lines = []
        for i, k in enumerate(self.contact_keys):
            c = self.contacts[k]
            prefix = "➤ " if k == self.current_key else "  "
            icon = "🔒" if c["connected"] else "🌐" # Candado o Mundo
            lines.append(f"{prefix}{icon} {c['name']}")
        self.w_contacts.text = "\n".join(lines)

        # 2. Renderizar Chat Activo
        if self.current_key:
            c = self.contacts[self.current_key]
            self.w_chat.text = "\n".join(c["msgs"])
            state = "CONECTADO" if c["connected"] else "SIN CONEXIÓN (Pulsa Enter para conectar)"
            self.w_chat.title = f"Chat con {c['name']} [{state}]"
        else:
            self.w_chat.text = "Esperando vecinos..."
            self.w_chat.title = "Chat Seguro"

        self.app.invalidate()

    async def run(self):
        await self.app.run_async()