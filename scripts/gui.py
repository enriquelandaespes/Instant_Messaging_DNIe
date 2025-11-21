# gui.py
import asyncio
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.data_structures import Point

class ChatGUI:
    def __init__(self, protocol, my_nick, db):
        self.protocol = protocol
        self.my_nick = my_nick
        self.db = db
        
        self.contacts = {} 
        self.contact_keys = [] 
        self.current_key = None 
        self.selected_idx = 0
        
        # Cargar historial al inicio
        self.cargar_historial_inicial()

        # --- WIDGETS ---
        self.w_contacts = TextArea(focusable=False, width=35)
        
        # Control personalizado para colores y alineación
        self.chat_control = FormattedTextControl(
            text=self._get_chat_content,
            get_cursor_position=lambda: Point(0, 10000), # Auto-scroll
            focusable=False 
        )
        self.w_chat_window = Window(
            content=self.chat_control, 
            wrap_lines=True, 
            always_hide_cursor=False
        )
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # --- LAYOUT ---
        self.layout = Layout(HSplit([
            VSplit([
                Frame(self.w_contacts, title="Vecinos (DNIe)"), 
                Frame(self.w_chat_window, title=self._get_chat_title)
            ]),
            Frame(self.w_input, title=f"Escribe (Enter) | 'c' conectar - Soy: {my_nick}")
        ]))

        # --- KEYBINDINGS ---
        kb = KeyBindings()
        @kb.add("c-c")
        def _(e): e.app.exit()
        @kb.add("up")
        def _(e): self.move_selection(-1)
        @kb.add("down")
        def _(e): self.move_selection(1)
        @kb.add("enter")
        def _(e): asyncio.create_task(self.handle_enter())
        @kb.add("c")
        def _(e): self.conectar_manual()

        self.app = Application(layout=self.layout, key_bindings=kb, full_screen=True, mouse_support=True)

    def _get_chat_title(self):
        if not self.current_key: return "Chat Seguro"
        c = self.contacts[self.current_key]
        status = "ONLINE" if c["connected"] else "OFFLINE"
        return f"Chat con {c['name']} [{status}]"

    def _get_chat_content(self):
        if not self.current_key: return []
        
        c = self.contacts[self.current_key]
        formatted_lines = []
        PAD_WIDTH = 80 # Ancho virtual para empujar texto a la derecha
        
        for m in c["msgs"]:
            sender = m["sender"]
            text = m["text"]
            status = m.get("status", "")
            
            formatted_lines.append(("", "\n")) # Salto de línea
            
            if sender == "yo":
                # --- MENSAJES PROPIOS (DERECHA - CYAN) ---
                # Lógica de Ticks
                if status == 'delivered': tick = "✓✓"
                elif status == 'sent': tick = "✓"
                else: tick = "🕒"
                
                line_content = f"{text}  {tick}"
                # Padding para empujar a la derecha
                padding = " " * max(0, PAD_WIDTH - len(line_content))
                
                formatted_lines.append(("", padding))
                formatted_lines.append(("ansicyan bold", line_content))
                
            elif sender == "sys":
                # --- SISTEMA (CENTRO - GRIS) ---
                center_pad = " " * max(0, (PAD_WIDTH - len(text)) // 2)
                formatted_lines.append(("ansigray", f"{center_pad}--- {text} ---"))
                
            else:
                # --- RECIBIDOS (IZQUIERDA - AMARILLO) ---
                formatted_lines.append(("ansiyellow", f"{sender}:\n"))
                formatted_lines.append(("ansiyellow", f" > {text}"))
                
        return formatted_lines

    def cargar_historial_inicial(self):
        hist = self.db.load_history()
        for key, data in hist.items():
            self.contacts[key] = {
                "name": data.get("name", "Desconocido"),
                "msgs": data.get("history", []), # Ya viene en formato correcto de DB
                "connected": False,
                "msg_queue": []
            }
        self.contact_keys = sorted(list(self.contacts.keys()))
        if self.contact_keys: self.current_key = self.contact_keys[0]

    def move_selection(self, delta):
        if not self.contact_keys: return
        idx = self.contact_keys.index(self.current_key) if self.current_key else 0
        self.current_key = self.contact_keys[(idx + delta) % len(self.contact_keys)]
        self.app.invalidate()

    def add_peer(self, name, ip, port):
        key = f"{ip}:{port}"
        if key in self.contacts:
            # Actualizar nombre si pasa de Nick mDNS a Nombre DNIe
            if "DNIe" in self.contacts[key]["name"] and "DNIe" not in name:
                 self.contacts[key]["name"] = name
            return 

        self.contacts[key] = {"name": name, "msgs": [], "connected": False, "msg_queue": []}
        if not self.contact_keys: self.current_key = key
        
        self.contact_keys = sorted(list(self.contacts.keys()))
        self.app.invalidate()

    def conectar_manual(self):
        if not self.current_key: return
        ip, port = self.current_key.split(":")
        self.add_msg_internal(self.current_key, "sys", "Iniciando Handshake manual...", "info")
        self.protocol.enviar_handshake(ip, int(port))

    def on_protocol_msg(self, addr, text, nombre):
        key = f"{addr[0]}:{addr[1]}"
        if key not in self.contacts: self.add_peer(nombre, addr[0], addr[1])
        
        c = self.contacts[key]
        
        if text == "HANDSHAKE_OK":
            c["connected"] = True
            c["name"] = nombre
            self.add_msg_internal(key, "sys", f"CONEXIÓN OK: {nombre}", "info")
            # Reenviar cola
            for txt in c["msg_queue"]:
                mid = self.db.save_message(c["name"], key, "yo", txt, "sent") # Guardar y obtener ID
                self.protocol.enviar_mensaje(addr[0], addr[1], txt, mid)
                self.add_msg_internal(key, "yo", txt, "sent", mid) # Actualizar UI
            c["msg_queue"] = []
        else:
            c["connected"] = True
            # Mensaje recibido
            self.db.save_message(nombre, key, nombre, text, "received")
            self.add_msg_internal(key, nombre, text, "received")

    def on_ack_received(self, sender_cn, msg_id):
        # Buscar mensaje por ID y actualizar a delivered
        # Nota: Esto requiere buscar en todos los contactos si no tenemos la key directa
        # Simplificación: Asumimos que sender_cn nos ayuda o refrescamos todo
        self.db.mark_message_status_by_id(msg_id, "delivered")
        
        # Actualizar memoria local UI
        for k, c in self.contacts.items():
            for m in c["msgs"]:
                if m.get("id") == msg_id: # Asumiendo que guardamos ID en memoria UI también
                    m["status"] = "delivered"
        
        self.app.invalidate()

    async def handle_enter(self):
        if not self.current_key: return
        text = self.w_input.text.strip()
        self.w_input.text = ""
        if not text: return

        c = self.contacts[self.current_key]
        ip, port = self.current_key.split(":")
        port = int(port)

        # Generar ID y guardar
        msg_id = self.db.save_message(c["name"], self.current_key, "yo", text, "pending")
        
        # Intentar enviar
        if self.protocol.enviar_mensaje(ip, port, text, msg_id):
            self.add_msg_internal(self.current_key, "yo", text, "sent", msg_id)
            c["connected"] = True
        else:
            c["connected"] = False
            c["msg_queue"].append(text)
            self.add_msg_internal(self.current_key, "yo", text, "pending", msg_id)
            # No mostramos error, simplemente encolamos
            self.add_msg_internal(self.current_key, "sys", "En cola (Conectando...)", "info")

    def add_msg_internal(self, key, sender, text, status, mid=None):
        if key not in self.contacts: return
        msg = {"sender": sender, "text": text, "status": status, "time": datetime.now().strftime("%H:%M")}
        if mid: msg["id"] = mid
        self.contacts[key]["msgs"].append(msg)
        self.app.invalidate()

    async def run(self):
        await self.app.run_async()