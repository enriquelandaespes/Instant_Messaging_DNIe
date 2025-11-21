# gui.py
import asyncio
from datetime import datetime
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
        
        self.contact_keys = [] 
        self.current_cn = None 
        self.pending_handshakes = set()

        # --- Widgets ---
        self.w_contacts = TextArea(focusable=False, width=35)
        
        # Control de chat visual
        self.chat_control = FormattedTextControl(
            text=self._get_chat_content,
            get_cursor_position=self._get_chat_cursor_position,
            focusable=False 
        )
        
        self.w_chat_window = Window(content=self.chat_control, wrap_lines=True, always_hide_cursor=False)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # --- Layout ---
        self.layout = Layout(
            HSplit([
                VSplit([
                    Frame(self.w_contacts, title="Contactos (DNIe)"), 
                    Frame(self.w_chat_window, title=self._get_chat_title)
                ]),
                Frame(self.w_input, title=f"Escribe ({my_nick})")
            ]),
            focused_element=self.w_input
        )

        kb = KeyBindings()
        @kb.add("c-c")
        def _(event): event.app.exit()
        @kb.add("up")
        def _(event): self.move_selection(-1)
        @kb.add("down")
        def _(event): self.move_selection(1)
        @kb.add("enter")
        def _(event): asyncio.create_task(self.handle_enter())

        self.app = Application(layout=self.layout, key_bindings=kb, full_screen=True, mouse_support=True)
        self._load_initial_contacts()

    def _get_chat_cursor_position(self):
        fragments = self._get_chat_content()
        full_text = "".join(item[1] for item in fragments)
        return Point(x=0, y=max(0, full_text.count('\n')))

    def _load_initial_contacts(self):
        for cn in self.db.get_all_contacts().keys():
            if cn not in self.contact_keys: self.contact_keys.append(cn)
        self.contact_keys.sort()
        if self.contact_keys and self.current_cn is None:
            self.current_cn = self.contact_keys[0]
        self.refresh_ui()

    def _get_chat_title(self):
        if not self.current_cn: return "Chat Seguro"
        info = self.db.get_contact_info(self.current_cn)
        status = "🔴 OFFLINE"
        if info and info.get("is_connected"): status = "🟢 CONECTADO"
        elif info and info.get("ip"): status = "🟡 DISPONIBLE"
        if self.current_cn in self.pending_handshakes: status = "⏳ CONECTANDO..."
        return f"Chat con {self.current_cn} [{status}]"

    def _get_chat_content(self):
        if not self.current_cn: return [("class:info", "Esperando contactos...")]
        msgs = self.db.get_history(self.current_cn)
        formatted_lines = []
        
        # Ancho virtual para calcular alineación (aprox)
        VIRTUAL_WIDTH = 80 
        
        for m in msgs:
            sender, text, time, status = m.get('sender'), m.get('text'), m.get('time'), m.get('status', '')
            
            # Separador vertical
            formatted_lines.append(("", "\n"))
            
            if sender == self.my_nick:
                # --- MENSAJES ENVIADOS (DERECHA) ---
                
                # Lógica de Ticks
                if status == 'delivered':
                    tick = "✓✓" # Confirmado por ACK
                elif status == 'sent':
                    tick = "✓"  # Enviado por UDP (sin confirmar)
                else:
                    tick = "🕒" # En cola / Pendiente
                
                # Construimos la línea: TEXTO  HORA  TICK
                line_content = f"{text}   {time} {tick}"
                
                # Calculamos espacios para empujar a la derecha
                padding_len = max(0, VIRTUAL_WIDTH - len(line_content))
                padding = " " * padding_len
                
                # Añadimos el padding y luego el texto en Cyan
                formatted_lines.append(("", padding))
                formatted_lines.append(("ansicyan bold", line_content))
                
            elif sender == "Sys":
                # --- MENSAJES SISTEMA (CENTRO) ---
                center_pad = " " * max(0, (VIRTUAL_WIDTH - len(text)) // 2)
                formatted_lines.append(("ansigray", f"{center_pad}--- {text} ---"))
                
            else:
                # --- MENSAJES RECIBIDOS (IZQUIERDA) ---
                # Formato: [Hora] Nombre: Texto
                formatted_lines.append(("ansiyellow", f"[{time}] {sender}:\n")) 
                formatted_lines.append(("ansiyellow", f" > {text}")) 
                
        return formatted_lines

    def move_selection(self, delta):
        if not self.contact_keys: return
        idx = self.contact_keys.index(self.current_cn) if self.current_cn in self.contact_keys else 0
        new_idx = (idx + delta) % len(self.contact_keys)
        self.current_cn = self.contact_keys[new_idx]
        self.refresh_ui()

    def add_or_update_peer(self, name, ip, port):
        real_exists = False
        for cn, data in self.db.get_all_contacts().items():
            if data.get("ip") == ip:
                real_exists = True
                break
        if not real_exists:
            self.db.add_or_update_contact(name, ip=ip, port=port)
            if name not in self.contact_keys:
                self.contact_keys.append(name)
                self.contact_keys.sort()
            if not self.current_cn: self.current_cn = name
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, real_cn):
        if real_cn in self.pending_handshakes: self.pending_handshakes.remove(real_cn)
        self.db.add_or_update_contact(real_cn, ip=addr[0], port=addr[1])
        if real_cn not in self.contact_keys:
            self.contact_keys.append(real_cn)
            self.contact_keys.sort()

        ts = datetime.now().strftime("%H:%M")
        
        if text == "HANDSHAKE_OK":
            self.db.set_contact_connected(real_cn, True)
            self.db.add_message(real_cn, "Sys", "CONEXIÓN SEGURA ESTABLECIDA", "system", ts)
            self.check_pending_messages(real_cn, addr[0], addr[1])
            
        elif text.startswith("HANDSHAKE_ERROR"):
            self.db.set_contact_connected(real_cn, False)
            self.db.add_message(real_cn, "Sys", f"ERROR: {text}", "error", ts)
            
        elif text == "ERROR_DESCIFRADO":
            self.db.add_message(real_cn, "Sys", "Error cifrado.", "error", ts)
            
        else:
            # Mensaje normal recibido
            self.db.set_contact_connected(real_cn, True)
            self.db.add_message(real_cn, real_cn, text, "received", ts)
            
        self.refresh_ui()

    def on_ack_received(self, cn, msg_id):
        # Cuando llega el ACK, actualizamos la DB a 'delivered'
        self.db.mark_message_status(cn, msg_id, "delivered")
        # Forzamos repintado para que salga el segundo tick
        self.app.invalidate()

    def check_pending_messages(self, cn, ip, port):
        pending = self.db.get_pending_messages(cn)
        if not pending: return
        ids = []
        for i, msg in pending:
            # Reintentar envío
            self.protocol.enviar_mensaje(ip, port, msg["text"], msg["id"])
            ids.append(i)
        if ids:
            # Pasamos a 'sent' (1 tick) esperando el ACK
            self.db.mark_message_status(cn, ids, "sent") 
            self.refresh_ui()

    async def handle_enter(self):
        if not self.current_cn: return
        text = self.w_input.text.strip()
        
        info = self.db.get_contact_info(self.current_cn)
        if not info: return 
        ip, port = info.get("ip"), info.get("port")
        ts = datetime.now().strftime("%H:%M")

        if not ip:
            if text:
                self.db.add_message(self.current_cn, "Sys", "Usuario Offline", "error", ts)
            self.w_input.text = ""
            self.refresh_ui()
            return

        if not info.get("is_connected"):
            if self.current_cn in self.pending_handshakes: return 
            self.protocol.enviar_handshake(ip, port)
            self.pending_handshakes.add(self.current_cn)
            
            self.db.add_message(self.current_cn, "Sys", "Iniciando conexión segura...", "system", ts)
            if text:
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", ts)
            
            self.w_input.text = ""
            self.refresh_ui()
            
            await asyncio.sleep(10)
            if self.current_cn in self.pending_handshakes:
                self.pending_handshakes.remove(self.current_cn)
                self.refresh_ui()
            return

        if text:
            # 1. Guardar como 'sent' (1 tick)
            msg_id = self.db.add_message(self.current_cn, self.my_nick, text, "sent", ts)
            # 2. Enviar
            if self.protocol.enviar_mensaje(ip, port, text, msg_id):
                self.w_input.text = ""
            else:
                self.db.set_contact_connected(self.current_cn, False)
                self.protocol.enviar_handshake(ip, port)
                
            self.refresh_ui()

    def refresh_ui(self):
        lines = []
        for k in self.contact_keys:
            info = self.db.get_contact_info(k)
            if not info: continue
            icon = "🟢" if info.get("is_connected") else ("🟡" if info.get("ip") else "🔴")
            prefix = "➤ " if k == self.current_cn else "  "
            lines.append(f"{prefix}{icon} {k}")
        self.w_contacts.text = "\n".join(lines)
        self.app.invalidate()

    async def run(self):
        await self.app.run_async()