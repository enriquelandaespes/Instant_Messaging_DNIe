# gui.py
import asyncio
import json
import os
import unicodedata
from datetime import datetime, timedelta
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.data_structures import Point
from prompt_toolkit.styles import Style

class ChatGUI:
    def __init__(self, protocol, my_nick, db, my_ip="0.0.0.0", my_port=0):
        self.protocol = protocol
        self.my_nick = my_nick
        self.db = db
        self.my_ip = my_ip
        self.my_port = my_port
        
        self.contact_keys = []
        self.current_cn = None
        self.pending_handshakes = set()
        self._timeout_check_task = None
        self._retry_pending_task = None
        
        self._last_line_count = 0
        self.scroll_offset = 0 
        
        self.ascii_art = {}
        try:
            ascii_path = os.path.join(os.path.dirname(__file__), 'ascii.json')
            with open(ascii_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.ascii_art = data.get('ascii', {})
        except Exception as e:
            print(f"Error cargando ascii.json: {e}")
        
        self.w_contacts = TextArea(focusable=False, width=35)
        
        self.chat_control = FormattedTextControl(
            text=self._get_chat_content,
            get_cursor_position=self._get_safe_cursor_position,
            focusable=False,
            show_cursor=False
        )
        
        self.w_chat_window = Window(
            content=self.chat_control,
            wrap_lines=True,
            always_hide_cursor=True,
            style="class:chat-bg",
            allow_scroll_beyond_bottom=False,
            dont_extend_height=False
        )
        
        self.w_ascii = TextArea(height=3, prompt="> ", multiline=False, width=35)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)
        self.w_suggestions = TextArea(focusable=False, height=1, style="fg:ansigray")
        
        def on_ascii_text_changed(_):
            self.update_ascii_suggestions()
        self.w_ascii.buffer.on_text_changed += on_ascii_text_changed

        self.layout = Layout(
            HSplit([
                VSplit([
                    Frame(self.w_contacts, title="👥 Contactos"),
                    Frame(self.w_chat_window, title=self._get_chat_title)
                ]),
                VSplit([
                    HSplit([
                        Frame(self.w_ascii, title=" ASCII Art "),
                        self.w_suggestions
                    ]),
                    Frame(self.w_input, title=f" Escribe aquí ")
                ])
            ]),
            focused_element=self.w_input
        )

        style = Style.from_dict({
            'chat-bg': '',
            'msg-sent': "#C3C3C3",     
            'msg-recv': '#ff8800',
            'msg-sys': '#888888 italic',
            'time': '#5599ff bold',              
            'tick-sent': '#aaaaaa',
            'tick-read': '#00ff00 bold',
        })

        kb = KeyBindings()
        @kb.add("c-c")
        def _(e): e.app.exit()
        @kb.add("up")
        def _(e): self.move_selection(-1)
        @kb.add("down")
        def _(e): self.move_selection(1)
        @kb.add("enter")
        def _(e): asyncio.create_task(self.handle_enter())
        @kb.add("c-d")
        def _(e): self.force_disconnect()
        
        @kb.add("tab")
        def _(e):
            if e.app.layout.has_focus(self.w_input):
                e.app.layout.focus(self.w_ascii)
            else:
                e.app.layout.focus(self.w_input)
        
        @kb.add("s-up")
        def _(e):
            self.scroll_offset += 5
            if self.scroll_offset > max(0, self._last_line_count - 1):
                self.scroll_offset = max(0, self._last_line_count - 1)
            e.app.invalidate()
        
        @kb.add("s-down")
        def _(e):
            self.scroll_offset -= 5
            if self.scroll_offset < 0:
                self.scroll_offset = 0
            e.app.invalidate()

        self.app = Application(layout=self.layout, key_bindings=kb, full_screen=True, mouse_support=True, style=style)
        self._load_initial_contacts()

    def _get_safe_cursor_position(self):
        if self._last_line_count <= 1:
            return Point(0, 0)
        max_offset = max(0, self._last_line_count - 1)
        actual_offset = min(self.scroll_offset, max_offset)
        target_line = max(0, self._last_line_count - 1 - actual_offset)
        return Point(x=0, y=target_line)

    def _load_initial_contacts(self):
        for cn in self.db.get_all_contacts().keys():
            if cn not in self.contact_keys: 
                self.contact_keys.append(cn)
        self.contact_keys.sort()
        if self.current_cn is None:
            self.current_cn = "__MI_CUENTA__"
        self.refresh_ui()

    def _get_chat_title(self):
        if not self.current_cn: 
            return "Chat Seguro"
        if self.current_cn == "__MI_CUENTA__": 
            return "👤 Mi Cuenta"
        if self.current_cn == "__AYUDA__": 
            return "❓ Ayuda - Atajos de Teclado"
        
        info = self.db.get_contact_info(self.current_cn)
        status = "🔴 OFFLINE"
        if info and info.get("is_connected"): 
            status = "🟢 CONECTADO"
        elif info and info.get("ip"): 
            status = "🟡 DISPONIBLE"
        if self.current_cn in self.pending_handshakes: 
            status = "⏳ CONECTANDO..."
        
        display_name = info.get("name", self.current_cn) if info else self.current_cn
        if ":" in self.current_cn:
            port = self.current_cn.split(":")[1]
            display_name = f"{display_name} [:{port}]"
        return f"Chat con {display_name} [{status}]"

    def _get_chat_content(self):
        if not self.current_cn:
            self._last_line_count = 1
            return [("class:msg-sys", "Esperando contactos...")]
        
        if self.current_cn == "__MI_CUENTA__": 
            return self._get_my_account_content()
        if self.current_cn == "__AYUDA__": 
            return self._get_help_content()
        
        msgs = list(self.db.get_history(self.current_cn))
        formatted_lines = []
        PAD_WIDTH = 80
        current_lines = 0
        
        for m in msgs:
            sender = m.get('sender')
            text = m.get('text')
            timestamp_str = m.get('timestamp')
            status = m.get('status', '')
            
            time = timestamp_str[11:16] if timestamp_str and len(timestamp_str) > 16 else "??:??"
            
            formatted_lines.append(("", "\n"))
            current_lines += 1
            
            if sender == "Sys":
                center_pad = " " * max(0, (PAD_WIDTH - self.visual_len(text)) // 2)
                formatted_lines.append(("class:msg-sys", f"{center_pad}--- {text} ---"))
            elif status == 'received' or sender != self.my_nick:
                formatted_lines.append(("class:msg-recv", f"[{time}] {sender}:\n"))
                current_lines += 1
                for line in text.split('\n'):
                    formatted_lines.append(("class:msg-recv", f" > {line}\n"))
                    current_lines += 1
            else:
                tick = "✅" if status == 'delivered' else "🕒"
                line_content = f"{text}   {time} {tick}"
                padding = " " * max(0, PAD_WIDTH - self.visual_len(line_content))
                
                formatted_lines.append(("", padding))
                formatted_lines.append(("class:msg-sent", text))
                formatted_lines.append(("class:time", f"   {time} "))
                tick_style = "class:tick-read" if status == 'delivered' else "class:tick-sent"
                formatted_lines.append((tick_style, f"{tick}\n"))
                current_lines += 1
        
        self._last_line_count = current_lines
        return formatted_lines

    def _get_my_account_content(self):
        formatted_lines = []
        formatted_lines.append(("", "\n\n"))
        formatted_lines.append(("class:msg-sys", "╔════════════════════════════════════════════════════════════════════════╗\n"))
        formatted_lines.append(("class:msg-sys", "║                         📄 MI CUENTA                                   ║\n"))
        formatted_lines.append(("class:msg-sys", "╚════════════════════════════════════════════════════════════════════════╝\n"))
        formatted_lines.append(("", "\n"))
        formatted_lines.append(("class:msg-recv", "👤 Usuario:\n"))
        formatted_lines.append(("class:msg-sent", f"   {self.my_nick}\n\n"))
        formatted_lines.append(("class:msg-recv", "🌐 Dirección IP:\n"))
        formatted_lines.append(("class:msg-sent", f"   {self.my_ip}\n\n"))
        formatted_lines.append(("class:msg-recv", "🔌 Puerto UDP:\n"))
        formatted_lines.append(("class:msg-sent", f"   {self.my_port}\n\n"))
        self._last_line_count = len(formatted_lines)
        return formatted_lines
    
    def _get_help_content(self):
        formatted_lines = []
        formatted_lines.append(("", "\n\n"))
        formatted_lines.append(("class:msg-sys", "--- AYUDA ---\n"))
        formatted_lines.append(("class:msg-recv", "Enter: Enviar / Shift+Flechas: Scroll / Tab: ASCII\n"))
        self._last_line_count = len(formatted_lines)
        return formatted_lines

    def visual_len(self, text):
        width = 0
        for char in text:
            ea = unicodedata.east_asian_width(char)
            width += 2 if ea in ('F', 'W') else 1
        return width

    def update_ascii_suggestions(self):
        current_text = self.w_ascii.text.strip().lower()
        if not current_text:
            self.w_suggestions.text = ""
            return
        matches = [key for key in self.ascii_art.keys() if current_text in key.lower()]
        if matches:
            suggestions_text = "  Sugerencias: " + ", ".join(matches[:5])
            if len(matches) > 5: 
                suggestions_text += " ..."
            self.w_suggestions.text = suggestions_text
        else:
            self.w_suggestions.text = "  Sin coincidencias"

    def refresh_ui(self):
        lines = []
        special_contacts = ["__MI_CUENTA__", "__AYUDA__"]
        
        for special in special_contacts:
            icon = "👤" if special == "__MI_CUENTA__" else "❓"
            display = "Mi Cuenta" if special == "__MI_CUENTA__" else "Ayuda"
            prefix = "➞ " if self.current_cn == special else "  "
            lines.append(f"{prefix}{icon} {display}")
        
        if self.contact_keys: 
            lines.append("─" * 32)
        
        for k in self.contact_keys:
            info = self.db.get_contact_info(k)
            if not info: 
                continue
            icon = "🟢" if info.get("is_connected") else ("🟡" if info.get("ip") else "🔴")
            prefix = "➞ " if k == self.current_cn else "  "
            display_name = info.get("name", k)
            if ":" in k:
                port = k.split(":")[1]
                display_name = f"{display_name} [:{port}]"
            unread = self.db.get_unread_count(k, self.my_nick)
            notify = f" 🔔({unread})" if unread > 0 else ""
            lines.append(f"{prefix}{icon} {display_name}{notify}")
        
        self.w_contacts.text = "\n".join(lines)
        self.app.invalidate()

    def move_selection(self, delta):
        all_items = ["__MI_CUENTA__", "__AYUDA__"] + self.contact_keys
        if not all_items: 
            return
        idx = all_items.index(self.current_cn) if self.current_cn in all_items else 0
        new_idx = (idx + delta) % len(all_items)
        self.current_cn = all_items[new_idx]
        self.scroll_offset = 0
        if self.current_cn not in ["__MI_CUENTA__", "__AYUDA__"]:
            self.db.mark_messages_as_read(self.current_cn, self.my_nick)
        self.refresh_ui()

    def add_peer(self, name, ip, port):
        """
        Añade un peer. Si ya hay clave guardada para ese CN/IP/port,
        NO se hace handshake nuevo: se llama a enviar_handshake con el CN,
        y protocol.py se encargará de usar PKT_RECONNECT en vez de INIT.
        """
        existing_cn = None
        all_contacts = self.db.get_all_contacts()
        
        # Buscar por IP+puerto
        for cn, info in all_contacts.items():
            if info.get("ip") == ip and info.get("port") == port:
                existing_cn = cn
                break
        
        # Buscar por nombre si no encontramos por IP
        if not existing_cn:
            target_name = name.strip().lower()
            for cn, info in all_contacts.items():
                if info.get("name", "").strip().lower() == target_name:
                    existing_cn = cn
                    break
        
        contact_id = existing_cn if existing_cn else f"{ip}:{port}"
        self.db.add_or_update_contact(contact_id, name=name, ip=ip, port=port)
        
        if contact_id not in self.contact_keys:
            self.contact_keys.append(contact_id)
            self.contact_keys.sort()
        
        # Intentar conectar (protocol.py decidirá si es handshake o reconnect)
        session_restored = self.protocol.enviar_handshake(ip, port, cn=contact_id)
        if session_restored:
            # Si se restauró la sesión desde la BD (envió PKT_RECONNECT), marcar conectado
            self.db.set_contact_connected(contact_id, True)
        
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, real_cn, msg_id=None):
        """
        Callback principal que recibe eventos del protocolo.
        """
        if text == "SESSIONS_READY":
            asyncio.create_task(self.auto_connect_and_send_all())
            return
        
        # Determinar el contact_id correcto
        contact_id = f"{addr[0]}:{addr[1]}"
        for cn, info in self.db.get_all_contacts().items():
            if info.get("ip") == addr[0] and info.get("port") == addr[1]:
                contact_id = cn
                break
        
        self.db.add_or_update_contact(contact_id, name=real_cn, ip=addr[0], port=addr[1])
        if contact_id not in self.contact_keys:
            self.contact_keys.append(contact_id)
            self.contact_keys.sort()

        ts = datetime.now().isoformat()
        
        if text == "HANDSHAKE_OK":
            # Handshake inicial completado
            self.db.set_contact_connected(contact_id, True)
            self.db.add_message(contact_id, "Sys", "🔒 Conexión segura establecida", "system", ts)
            if contact_id in self.pending_handshakes:
                self.pending_handshakes.discard(contact_id)
            self.check_pending_messages(contact_id, addr[0], addr[1])
        
        elif text == "SESSION_RESTORED":
            # Sesión restaurada desde BD sin handshake
            self.db.set_contact_connected(contact_id, True)
            self.db.add_message(contact_id, "Sys", "🔄 Sesión restaurada (sin handshake)", "system", ts)
            if contact_id in self.pending_handshakes:
                self.pending_handshakes.discard(contact_id)
            self.check_pending_messages(contact_id, addr[0], addr[1])
        
        elif text == "PEER_RECONNECTED":
            # Peer confirmó reconexión
            self.db.set_contact_connected(contact_id, True)
            self.db.add_message(contact_id, "Sys", "🔄 Peer reconectado", "system", ts)
        
        elif text.startswith("ACK|"):
            # Confirmación de recepción de mensaje
            ack_id = text.split("|", 1)[1]
            self.db.mark_message_status(contact_id, ack_id, "delivered")
        
        else:
            # Mensaje normal recibido
            self.db.set_contact_connected(contact_id, True)
            received_msg_id = self.db.add_message(contact_id, real_cn, text, "received", ts, msg_id=msg_id)
            
            # AUTO-ACK: Responder para que el otro deje de reintentar
            if msg_id:
                self.protocol.enviar_mensaje(addr[0], addr[1], f"ACK|{msg_id}")
            
            if self.current_cn == contact_id:
                self.db.mark_message_as_read_by_id(contact_id, received_msg_id)
        
        self.refresh_ui()

    async def _auto_retry_pending(self):
        """
        Reintenta envío de mensajes pendientes cada 3 segundos.
        Si se perdió la sesión, intenta reconectar (usando PKT_RECONNECT si hay clave guardada).
        """
        while True:
            await asyncio.sleep(3)
            for cn in list(self.contact_keys):
                pending = self.db.get_pending_messages(cn)
                if not pending: 
                    continue
                info = self.db.get_contact_info(cn)
                if not info: 
                    continue
                ip, port = info.get("ip"), info.get("port")
                if not ip: 
                    continue
                
                # Si no hay sesión activa, intentar reconectar
                if not self.protocol.tiene_sesion(ip, port):
                    # enviar_handshake decidirá si es handshake o reconnect
                    self.protocol.enviar_handshake(ip, port, cn=cn)
                else:
                    # Si hay sesión, reintentar envío
                    self.check_pending_messages(cn, ip, port)
            self.refresh_ui()

    def check_pending_messages(self, cn, ip, port):
        """
        Envía todos los mensajes pendientes si hay sesión activa.
        """
        pending = self.db.get_pending_messages(cn)
        if not pending or not self.protocol.tiene_sesion(ip, port): 
            return
        
        for msg in pending:
            # Usar el msg_id original para que el ACK coincida
            self.protocol.enviar_mensaje(ip, port, msg['text'], msg_id=msg['id'])

    async def handle_enter(self):
        """
        Manejo de Enter: envío de mensaje o conexión inicial.
        """
        if self.current_cn in ["__MI_CUENTA__", "__AYUDA__"]: 
            return
        
        # Determinar si estamos en ASCII o input normal
        if self.app.layout.has_focus(self.w_ascii):
            text = self.w_ascii.text.strip()
            if text in self.ascii_art:
                text = self.ascii_art[text]
                self.w_ascii.text = ""
        else:
            text = self.w_input.text.strip()
            self.w_input.text = ""

        info = self.db.get_contact_info(self.current_cn)
        if not info: 
            return
        ip, port = info.get("ip"), info.get("port")
        ts = datetime.now().isoformat()
        
        if not ip:
            self.db.add_message(self.current_cn, "Sys", "Usuario Offline - Sin IP", "error", ts)
            self.refresh_ui()
            return

        # Comprobar si hay sesión activa
        tiene_sesion = self.protocol.tiene_sesion(ip, port)

        if not tiene_sesion:
            # No hay sesión activa: guardar mensaje como pendiente e iniciar conexión
            if text:
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", ts)
            if self.current_cn not in self.pending_handshakes:
                # enviar_handshake decidirá si hacer handshake inicial o PKT_RECONNECT
                self.protocol.enviar_handshake(ip, port, cn=self.current_cn)
                self.pending_handshakes.add(self.current_cn)
                self.refresh_ui()
            return
        
        # Hay sesión activa: enviar mensaje
        if text:
            # Guardar como pending inicialmente
            msg_id = self.db.add_message(self.current_cn, self.my_nick, text, "pending", ts)
            
            # Intentar envío con el ID para recibir ACK
            if self.protocol.enviar_mensaje(ip, port, text, msg_id=msg_id):
                # Cambiar a 'sent' (reloj). Esperamos ACK para 'delivered' (tick verde)
                self.db.mark_message_status(self.current_cn, msg_id, "sent")
            else:
                self.db.add_message(self.current_cn, "Sys", "Error envío", "error", ts)
            
            self.refresh_ui()

<<<<<<< HEAD
=======
    def force_disconnect(self):
        if not self.current_cn:
            return
        info = self.db.get_contact_info(self.current_cn)
        if info and info.get("ip"):
            self.protocol.cerrar_sesion(info["ip"], info["port"])
            self.db.set_contact_connected(self.current_cn, False)
            ts = datetime.now().strftime("%H:%M")
            self.db.add_message(self.current_cn, "Sys", "Desconectado manualmente", "system", ts)
            self.refresh_ui()

    async def auto_connect_and_send_all(self):
        await asyncio.sleep(0.5)
        all_contacts = self.db.get_all_contacts()
        
        # Enviar PKT_RECONNECT a todos los contactos para avisar que estamos disponibles
        for cn, info in all_contacts.items():
            ip = info.get("ip")
            port = info.get("port")
            
            if not ip or not port:
                continue
            
            # Intentar restaurar sesión desde BD y enviar PKT_RECONNECT
            self.protocol.enviar_handshake(ip, port, cn=cn)
            await asyncio.sleep(0.1)
         
        self.refresh_ui()

>>>>>>> 0498593343e1ebab1672d6a1b0629a019cc25716
    async def check_ack_timeouts(self):
        """
        Controla que si un mensaje lleva >5s como 'sent' sin ACK, vuelva a 'pending' para reenvío.
        """
        while True:
            await asyncio.sleep(2)
            for cn in list(self.contact_keys):
                info = self.db.get_contact_info(cn)
                if info and info.get("is_connected"):
                    has_timeout = self.db.check_message_timeouts(cn, timeout_seconds=5)
                    if has_timeout:
                        self.refresh_ui()

    def force_disconnect(self):
        """
        Cerrar la sesión del contacto actual manualmente.
        """
        if self.current_cn in ["__MI_CUENTA__", "__AYUDA__"]: 
            return
        info = self.db.get_contact_info(self.current_cn)
        if not info: 
            return
        ip, port = info.get("ip"), info.get("port")
        if ip:
            self.protocol.cerrar_sesion(ip, port)
            self.db.set_contact_connected(self.current_cn, False)
            ts = datetime.now().isoformat()
            self.db.add_message(self.current_cn, "Sys", "Desconectado manualmente", "system", ts)
            self.refresh_ui()

    async def auto_connect_and_send_all(self):
        """
        Al arrancar, intenta reconectar con todos los contactos conocidos.
        Si tienen session_key guardada, usará PKT_RECONNECT; si no, handshake inicial.
        """
        await asyncio.sleep(1)
        for cn in list(self.contact_keys):
            info = self.db.get_contact_info(cn)
            if not info:
                continue
            ip, port = info.get("ip"), info.get("port")
            if not ip:
                continue
            # enviar_handshake decidirá automáticamente si es handshake o reconnect
            self.protocol.enviar_handshake(ip, port, cn=cn)
        self.refresh_ui()

    async def run(self):
        """
        Arranca la GUI y las tareas en background.
        """
        self._timeout_check_task = asyncio.create_task(self.check_ack_timeouts())
        self._retry_pending_task = asyncio.create_task(self._auto_retry_pending())
        try:
            await self.app.run_async()
        finally:
            if self._timeout_check_task:
                self._timeout_check_task.cancel()
            if self._retry_pending_task:
                self._retry_pending_task.cancel()
