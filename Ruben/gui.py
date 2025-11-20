# gui.py
import asyncio
from datetime import datetime
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML

class ChatGUI:
    def __init__(self, protocol, my_nick, db):
        self.protocol = protocol
        self.my_nick = my_nick
        self.db = db
        
        # Contactos en memoria para la interfaz
        self.contacts_state = {} # { "CN": { "connected": Bool, "display_name": "..." } }
        self.current_cn = None
        self.contact_keys = []

        # --- Widgets ---
        self.w_contacts = TextArea(focusable=False, width=30)
        
        # Usamos Window + FormattedTextControl para tener COLORES y FORMATO
        self.chat_control = FormattedTextControl(text=self._get_chat_content)
        self.w_chat_window = Window(content=self.chat_control, wrap_lines=True)
        
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # Layout
        self.layout = Layout(HSplit([
            VSplit([
                Frame(self.w_contacts, title="Contactos (DNIe)"), 
                Frame(self.w_chat_window, title=self._get_chat_title)
            ]),
            Frame(self.w_input, title=f"Mensaje ({my_nick})")
        ]))

        # Cargar contactos previos de la BD JSON
        self._load_contacts_from_db()

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

    def _load_contacts_from_db(self):
        for cn in self.db.data["contacts"]:
            if cn not in self.contacts_state:
                self.contacts_state[cn] = {"connected": False, "display_name": cn.split(' ')[0]}
                self.contact_keys.append(cn)
        if self.contact_keys:
            self.current_cn = self.contact_keys[0]
        self.refresh_ui()

    def _get_chat_title(self):
        if not self.current_cn: return "Chat Seguro"
        state = self.contacts_state[self.current_cn]
        status = "ONLINE 🟢" if state["connected"] else "OFFLINE 🔴"
        return f"Chat con {state['display_name']} [{status}]"

    def _get_chat_content(self):
        if not self.current_cn: return [("class:info", "Esperando contactos...")]
        
        msgs = self.db.get_history(self.current_cn)
        formatted_lines = []
        
        # Ancho estimado para alinear a la derecha (parche visual)
        # prompt_toolkit no tiene "align right" nativo simple en FormattedText
        PAD_WIDTH = 80 
        
        for m in msgs:
            sender = m['sender']
            text = m['text']
            time = m['time']
            status = m['status']
            
            if sender == "Yo":
                # Mensaje Propio (Derecha, Color Verde Cyan)
                # Ticks: 1 (pending) = ✓, 2 (sent) = ✓✓
                ticks = "✓" if status == 'pending' else "✓✓"
                line_content = f"{text} {time} {ticks}"
                padding = " " * max(0, PAD_WIDTH - len(line_content))
                
                formatted_lines.append(("", "\n"))
                formatted_lines.append(("", padding))
                formatted_lines.append(("ansicyan", f"{line_content}"))
            elif sender == "Sys":
                # Sistema (Centro, Gris)
                line_content = f"--- {text} ---"
                formatted_lines.append(("", "\n"))
                formatted_lines.append(("ansigray", line_content.center(PAD_WIDTH)))
            else:
                # Otro (Izquierda, Amarillo)
                formatted_lines.append(("", "\n"))
                formatted_lines.append(("ansiyellow", f"[{time}] {sender}: {text}"))
                
        return formatted_lines

    def move_selection(self, delta):
        if not self.contact_keys: return
        idx = self.contact_keys.index(self.current_cn) if self.current_cn in self.contact_keys else 0
        new_idx = (idx + delta) % len(self.contact_keys)
        self.current_cn = self.contact_keys[new_idx]
        self.refresh_ui()

    def add_or_update_peer(self, name_or_cn, ip, port, is_cn=False):
        # Si no es CN (viene de Discovery como Ruben_6666), buscamos si ya existe
        cn = name_or_cn
        display = name_or_cn.split('_')[0]

        if not is_cn:
            # Intentar deducir o usar temporal
            cn = name_or_cn 
        
        if cn not in self.contacts_state:
            self.contacts_state[cn] = {"connected": False, "display_name": display}
            self.contact_keys.append(cn)
            if self.current_cn is None: self.current_cn = cn

        # Guardar IP en BD
        self.db.update_contact_info(cn, ip, port)
        
        # Intentar enviar pendientes si hay IP
        self.check_pending(cn, ip, port)
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, nombre_cn):
        # Actualizar estado visual a CONECTADO 🟢
        self.add_or_update_peer(nombre_cn, addr[0], addr[1], is_cn=True)
        self.contacts_state[nombre_cn]["connected"] = True
        self.contacts_state[nombre_cn]["display_name"] = nombre_cn # Nombre real del cert
        
        timestamp = datetime.now().strftime("%H:%M")
        
        if text == "HANDSHAKE_OK":
            self.db.add_message(nombre_cn, "Sys", "CONEXIÓN SEGURA ESTABLECIDA", "received", timestamp)
            self.check_pending(nombre_cn, addr[0], addr[1])
        else:
            self.db.add_message(nombre_cn, nombre_cn, text, "received", timestamp)
        
        self.refresh_ui()

    def check_pending(self, cn, ip, port):
        """Revisa mensajes con un solo tick y los intenta enviar."""
        pending = self.db.get_pending_messages(cn)
        if not pending: return
        
        sent_indices = []
        for i, msg in pending:
            self.protocol.enviar_mensaje(ip, port, msg["text"])
            sent_indices.append(i)
        
        if sent_indices:
            self.db.mark_as_sent(cn, sent_indices)

    def handle_enter(self):
        if not self.current_cn: return
        text = self.w_input.text.strip()
        self.w_input.text = ""
        if not text: return

        state = self.contacts_state[self.current_cn]
        # Recuperar IP de la BD
        if self.current_cn in self.db.data["contacts"]:
            ip = self.db.data["contacts"][self.current_cn].get("ip")
            port = self.db.data["contacts"][self.current_cn].get("port")
        else:
            ip, port = None, None

        timestamp = datetime.now().strftime("%H:%M")

        # Lógica:
        # 1. Si no hay IP -> Pendiente (1 Tick)
        # 2. Si hay IP pero no conectado -> Mandar Handshake + Guardar Pendiente (1 Tick)
        # 3. Si conectado -> Enviar + Guardar Enviado (2 Ticks)

        if not ip:
            self.db.add_message(self.current_cn, "Yo", text, "pending", timestamp)
        elif not state["connected"]:
            self.protocol.enviar_handshake(ip, port)
            self.db.add_message(self.current_cn, "Yo", text, "pending", timestamp)
        else:
            self.protocol.enviar_mensaje(ip, port, text)
            self.db.add_message(self.current_cn, "Yo", text, "sent", timestamp) # sent = 2 ticks

        self.refresh_ui()

    def refresh_ui(self):
        # Lista Contactos
        lines = []
        for k in self.contact_keys:
            s = self.contacts_state[k]
            prefix = "➤ " if k == self.current_cn else "  "
            
            # Lógica visual del estado
            if s["connected"]:
                icon = "🟢" 
            else:
                # Si tenemos IP en BD es "Disponible pero desconectado", si no "Desconocido"
                has_ip = False
                if k in self.db.data["contacts"] and self.db.data["contacts"][k].get("ip"):
                    has_ip = True
                icon = "🔴" if has_ip else "⚫"

            lines.append(f"{prefix}{icon} {s['display_name']}")
        
        self.w_contacts.text = "\n".join(lines)
        self.app.invalidate()

    async def run(self):
        await self.app.run_async()