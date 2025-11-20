# gui.py
import asyncio
from datetime import datetime
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.document import Document
from prompt_toolkit.data_structures import Point

class ChatGUI:
    def __init__(self, protocol, my_nick, db):
        self.protocol = protocol
        self.my_nick = my_nick
        self.db = db
        
        self.contacts_state = {} 
        self.current_cn = None
        self.contact_keys = []

        # --- Widgets ---
        self.w_contacts = TextArea(focusable=False, width=35)
        
        # --- CORRECCIÓN ESCRITURA ---
        # Quitamos focusable=True para que no robe el foco al escribir.
        # Mantenemos get_cursor_position para el auto-scroll.
        self.chat_control = FormattedTextControl(
            text=self._get_chat_content,
            get_cursor_position=self._get_chat_cursor_position,
            focusable=False 
        )
        
        self.w_chat_window = Window(content=self.chat_control, wrap_lines=True, always_hide_cursor=False)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        self.layout = Layout(HSplit([
            VSplit([
                Frame(self.w_contacts, title="Vecinos (DNIe)"), 
                Frame(self.w_chat_window, title=self._get_chat_title)
            ]),
            Frame(self.w_input, title=f"Escribe aquí ({my_nick})")
        ]))

        kb = KeyBindings()
        @kb.add("c-c")
        def _(event): event.app.exit()
        @kb.add("up")
        def _(event): self.move_selection(-1)
        @kb.add("down")
        def _(event): self.move_selection(1)
        @kb.add("enter")
        def _(event): self.handle_enter()

        # --- CORRECCIÓN FINAL: initial_focus ---
        # Esto asegura que al arrancar, el cursor esté listo para escribir
        self.app = Application(
            layout=self.layout, 
            key_bindings=kb, 
            full_screen=True, 
            mouse_support=True,
            initial_focus=self.w_input 
        )
        
        self._load_contacts_from_db()

    def _get_chat_cursor_position(self):
        # Truco para Auto-Scroll: Le decimos que el cursor virtual está siempre al final
        lines = self._get_chat_content()
        row_count = 0
        for item in lines:
            if item[1] == "\n": row_count += 1
        return Point(x=0, y=row_count)

    def _load_contacts_from_db(self):
        for cn in self.db.data["contacts"]:
            if cn not in self.contacts_state:
                self.contacts_state[cn] = {"connected": False, "display_name": cn, "ip": None}
                self.contact_keys.append(cn)
        if self.contact_keys and self.current_cn is None:
            self.current_cn = self.contact_keys[0]
        self.refresh_ui()

    def _get_chat_title(self):
        if not self.current_cn: return "Chat Seguro"
        state = self.contacts_state[self.current_cn]
        
        # --- LÓGICA DE ESTADOS CORREGIDA ---
        if state["connected"]:
             status = "🟢 CONECTADO"
        elif state.get("ip"):
             status = "🟡 DISPONIBLE (Pulsa Enter)"
        else:
             status = "🔴 OFFLINE"
             
        return f"Chat con {state['display_name']} [{status}]"

    def _get_chat_content(self):
        if not self.current_cn: return [("class:info", "Esperando contactos...")]
        
        msgs = self.db.get_history(self.current_cn)
        formatted_lines = []
        PAD_WIDTH = 80 
        
        for m in msgs:
            sender = m['sender']
            text = m['text']
            time = m['time']
            status = m['status']
            
            if sender == "Yo":
                ticks = "✓" if status == 'pending' else "✓✓"
                line_content = f"{text}   {time} {ticks}"
                padding = " " * max(0, PAD_WIDTH - len(line_content))
                formatted_lines.append(("", "\n"))
                formatted_lines.append(("", padding))
                formatted_lines.append(("ansicyan bold", f"{line_content}"))
            elif sender == "Sys":
                line_content = f"--- {text} ---"
                formatted_lines.append(("", "\n"))
                formatted_lines.append(("ansigray", line_content.center(PAD_WIDTH)))
            else:
                formatted_lines.append(("", "\n"))
                formatted_lines.append(("ansiyellow", f"[{time}] {sender}: {text}"))
                
        return formatted_lines

    def move_selection(self, delta):
        if not self.contact_keys: return
        idx = 0
        if self.current_cn in self.contact_keys:
            idx = self.contact_keys.index(self.current_cn)
        new_idx = (idx + delta) % len(self.contact_keys)
        self.current_cn = self.contact_keys[new_idx]
        self.refresh_ui()

    def add_or_update_peer(self, name_or_cn, ip, port, is_cn=False):
        target_cn = None
        
        # 1. Búsqueda exacta
        if name_or_cn in self.contacts_state:
            target_cn = name_or_cn
        else:
            # 2. Búsqueda por coincidencia de inicio (para discovery)
            prefix = name_or_cn.split('_')[0]
            for k in self.contact_keys:
                if k.startswith(prefix):
                    target_cn = k
                    break
            
            if not target_cn and not is_cn and ip is not None:
                target_cn = name_or_cn # Nuevo contacto temporal

        if not target_cn: return

        if target_cn not in self.contacts_state:
            self.contacts_state[target_cn] = {"connected": False, "display_name": target_cn, "ip": None}
            self.contact_keys.append(target_cn)
            if self.current_cn is None: self.current_cn = target_cn

        # Actualización de estado
        if ip is None:
            # Se ha ido
            self.contacts_state[target_cn]["connected"] = False
            self.contacts_state[target_cn]["ip"] = None
            self.db.update_contact_info(target_cn, None, None)
        else:
            # Ha aparecido
            self.contacts_state[target_cn]["ip"] = ip
            self.db.update_contact_info(target_cn, ip, port)
            
            # Solo marcamos "Conectado" (Verde) si es un mensaje real de protocolo, 
            # si viene de Discovery se queda en Amarillo (Disponible)
            if is_cn: 
                self.contacts_state[target_cn]["connected"] = True
                self.contacts_state[target_cn]["display_name"] = name_or_cn # Nombre limpio
            
            self.check_pending(target_cn, ip, port)

        self.refresh_ui()

    def on_protocol_msg(self, addr, text, nombre_cn):
        # Al recibir mensaje, confirmamos conexión verde
        self.add_or_update_peer(nombre_cn, addr[0], addr[1], is_cn=True)
        
        timestamp = datetime.now().strftime("%H:%M")
        
        if text == "HANDSHAKE_OK":
            self.db.add_message(nombre_cn, "Sys", "CONEXIÓN SEGURA ESTABLECIDA", "received", timestamp)
            self.check_pending(nombre_cn, addr[0], addr[1])
        else:
            self.db.add_message(nombre_cn, nombre_cn, text, "received", timestamp)
        
        self.refresh_ui()

    def check_pending(self, cn, ip, port):
        pending = self.db.get_pending_messages(cn)
        if not pending or not ip: return
        
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
        ip = state.get("ip")
        port = None
        
        if not ip and self.current_cn in self.db.data["contacts"]:
             ip = self.db.data["contacts"][self.current_cn].get("ip")
             port = self.db.data["contacts"][self.current_cn].get("port")
        
        if not port: port = 6666 
        timestamp = datetime.now().strftime("%H:%M")

        # Lógica de envío
        if not ip:
            # Caso Rojo
            self.db.add_message(self.current_cn, "Yo", text, "pending", timestamp)
        elif not state["connected"]:
            # Caso Amarillo (Intentamos handshake y guardamos pendiente)
            self.protocol.enviar_handshake(ip, port)
            self.db.add_message(self.current_cn, "Yo", text, "pending", timestamp)
        else:
            # Caso Verde
            self.protocol.enviar_mensaje(ip, port, text)
            self.db.add_message(self.current_cn, "Yo", text, "sent", timestamp)

        self.refresh_ui()

    def refresh_ui(self):
        lines = []
        for k in self.contact_keys:
            s = self.contacts_state[k]
            prefix = "➤ " if k == self.current_cn else "  "
            
            # Iconos
            if s["connected"]:
                icon = "🟢" # Verde
            elif s.get("ip"): 
                icon = "🟡" # Amarillo (Disponible)
            else:
                icon = "🔴" # Rojo (No IP)

            lines.append(f"{prefix}{icon} {s['display_name']}")
        
        self.w_contacts.text = "\n".join(lines)
        self.app.invalidate()

    async def run(self):
        await self.app.run_async()