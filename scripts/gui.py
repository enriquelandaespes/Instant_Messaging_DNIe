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
from prompt_toolkit.layout import ScrollablePane

class ChatGUI:
    def __init__(self, protocol, my_nick, db):
        self.protocol = protocol
        self.my_nick = my_nick
        self.db = db
        
        self.contact_keys = []
        self.current_cn = None
        self.pending_handshakes = set()
        self._timeout_check_task = None
        
        # Cargar ASCII art
        self.ascii_art = {}
        try:
            ascii_path = os.path.join(os.path.dirname(__file__), 'ascii.json')
            with open(ascii_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.ascii_art = data.get('ascii', {})
        except Exception as e:
            print(f"Error cargando ascii.json: {e}")
        
        self.w_contacts = TextArea(focusable=False, width=35)
        self._chat_control = FormattedTextControl(text="")
        self.w_chat = ScrollablePane(Window(content=self._chat_control, wrap_lines=True))
        self.w_ascii = TextArea(height=3, prompt="> ", multiline=False,width=35)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)
        self.w_suggestions = TextArea(focusable=False, height=1, style="fg:ansigray")
        
        # Listener para autocompletado en tiempo real
        def on_ascii_text_changed(_):
            self._update_ascii_suggestions()
        self.w_ascii.buffer.on_text_changed += on_ascii_text_changed

        self.layout = Layout(
            HSplit([
                VSplit([
                    Frame(self.w_contacts, title="👥 Contactos"),
                    Frame(self.w_chat, title=self._get_chat_title)
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

        kb = KeyBindings()
        @kb.add("c-c")
        def _(event): event.app.exit()
        @kb.add("up")
        def _(event): self.move_selection(-1)
        @kb.add("down")
        def _(event): self.move_selection(1)
        @kb.add("enter")
        def _(event): asyncio.create_task(self.handle_enter())
        @kb.add("c-d")
        def _(event): self.force_disconnect()
        @kb.add("tab")
        def _(event):
            # Cambiar entre w_input y w_ascii
            if event.app.layout.has_focus(self.w_input):
                event.app.layout.focus(self.w_ascii)
            else:
                event.app.layout.focus(self.w_input)

        from prompt_toolkit.styles import Style
        custom_style = Style.from_dict({
            'date-separator': 'fg:ansigray italic',
            'time-small': 'fg:ansigray',
            'time-small-sent': 'fg:#666666',
            'msg-received': 'fg:#ff8800',  # Naranja para recibidos
            'msg-sent': 'fg:#5599ff',      # Azul para enviados
            'msg-system': 'fg:#888888',    # Gris para sistema
        })
        
        self.app = Application(layout=self.layout, key_bindings=kb, full_screen=True, mouse_support=True, style=custom_style)
        self._load_initial_contacts()

    def _format_timestamp(self, time_str, full_date_str=None):
        try:
            if full_date_str:
                msg_datetime = datetime.strptime(full_date_str, "%Y-%m-%d %H:%M")
            else:
                msg_datetime = datetime.strptime(f"{datetime.now().strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %H:%M")
            now = datetime.now()
            today = now.date()
            msg_date = msg_datetime.date()
            if msg_date == today:
                return f"Hoy {time_str}"
            elif msg_date == today - timedelta(days=1):
                return f"Ayer {time_str}"
            elif msg_date.year == today.year:
                return msg_datetime.strftime(f"%d %b {time_str}")
            else:
                return msg_datetime.strftime(f"%d/%m/%y {time_str}")
        except:
            return time_str

    def _visual_len(self, text):
        """Calcula el ancho visual de una cadena, considerando emojis y caracteres especiales"""
        width = 0
        for char in text:
            ea = unicodedata.east_asian_width(char)
            if ea in ('F', 'W'):  # Fullwidth o Wide
                width += 2
            elif ea in ('Na', 'H', 'N', 'A'):  # Narrow, Halfwidth, Neutral, Ambiguous
                width += 1
            else:
                width += 1
        return width

    def _update_ascii_suggestions(self):
        """Muestra sugerencias de ASCII art mientras escribes"""
        current_text = self.w_ascii.text.strip().lower()
        if not current_text:
            self.w_suggestions.text = ""
            return
        
        # Buscar coincidencias
        matches = [key for key in self.ascii_art.keys() if current_text in key.lower()]
        
        if matches:
            # Mostrar hasta 5 sugerencias
            suggestions_text = "  Sugerencias: " + ", ".join(matches[:5])
            if len(matches) > 5:
                suggestions_text += f" ... (+{len(matches)-5} más)"
            self.w_suggestions.text = suggestions_text
        else:
            self.w_suggestions.text = "  Sin coincidencias"

    def _load_initial_contacts(self):
        for cn in self.db.get_all_contacts().keys():
            if cn not in self.contact_keys:
                self.contact_keys.append(cn)
        self.contact_keys.sort()
        if self.contact_keys and self.current_cn is None:
            self.current_cn = self.contact_keys[0]
        self.refresh_ui()

    def _get_chat_title(self):
        if not self.current_cn:
            return "Chat Seguro"
        info = self.db.get_contact_info(self.current_cn)
        status = "🔴 OFFLINE"
        if info and info.get("is_connected"):
            status = "🟢 CONECTADO"
        elif info and info.get("ip"):
            status = "🟡 DISPONIBLE"
        if self.current_cn in self.pending_handshakes:
            status = "⏳ CONECTANDO..."
        full_name = info.get("name", self.current_cn) if info else self.current_cn
        name_parts = full_name.split()
        if len(name_parts) >= 2:
            if "," in full_name:
                parts = full_name.split(",")
                apellidos = parts[0].strip().split()
                nombre = parts[1].strip().split()[0] if len(parts) > 1 else ""
                display_name = f"{nombre} {apellidos[0]}"
            else:
                display_name = f"{name_parts[0]} {name_parts[1]}"
        else:
            display_name = full_name
        if ":" in self.current_cn:
            port = self.current_cn.split(":")[1]
            display_name = f"{display_name} [:{port}]"
        return f"Chat con {display_name} [{status}]"

    def _get_chat_content(self):
        if not self.current_cn:
            return "Esperando contactos..."
        msgs = list(self.db.get_history(self.current_cn))
        lines = []
        PAD_WIDTH = 80
        last_date = None
        for m in msgs:
            sender = m.get('sender')
            text = m.get('text')
            timestamp_iso = m.get('timestamp')
            status = m.get('status', '')
            if timestamp_iso:
                try:
                    dt = datetime.fromisoformat(timestamp_iso)
                    time = dt.strftime("%H:%M")
                    full_date = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    time = timestamp_iso[:5] if len(timestamp_iso) >= 5 else "??:??"
                    full_date = None
            else:
                time = "??:??"
                full_date = None
            formatted_time = self._format_timestamp(time, full_date)
            current_date = formatted_time.split()[0] if formatted_time and ' ' in formatted_time else None
            if current_date and last_date != current_date and current_date != time:
                if last_date is not None:
                    lines.append("")
                separator = f"- {current_date} -"
                center_pad = " " * max(0, (PAD_WIDTH - len(separator)) // 2)
                lines.append(f"{center_pad}{separator}")
                last_date = current_date
            lines.append("")
            if sender == "Sys":
                center_pad = " " * max(0, (PAD_WIDTH - self._visual_len(text)) // 2)
                lines.append(f"{center_pad}<msg-system>--- {text} ---</msg-system>")
            elif status == 'received' or sender != self.my_nick:
                lines.append(f"<msg-received>[{formatted_time}] {sender}:</msg-received>")
                # Manejar mensajes multilínea (ASCII art)
                for line in text.split('\n'):
                    lines.append(f" > {line}")
            else:
                if status == 'delivered':
                    tick = "✅"
                elif status == 'sent':
                    tick = "🕒"
                elif status == 'pending':
                    tick = "🕒"
                else:
                    tick = "🕒"
                
                time_and_tick = f"{formatted_time} {tick}"
                
                # Manejar mensajes multilínea (ASCII art enviados)
                text_lines = text.split('\n')
                if len(text_lines) > 1:
                    # ASCII art multilínea - alinear cada línea a la derecha
                    for i, line in enumerate(text_lines):
                        if i == len(text_lines) - 1:
                            # Última línea con timestamp
                            line_content = f"{line}   {time_and_tick}"
                            visual_width = self._visual_len(line) + 3 + self._visual_len(time_and_tick)
                            padding = " " * max(0, PAD_WIDTH - visual_width)
                            lines.append(f"{padding}{line}   <msg-sent>{time_and_tick}</msg-sent>")
                        else:
                            # Líneas intermedias sin timestamp
                            padding = " " * max(0, PAD_WIDTH - self._visual_len(line))
                            lines.append(f"{padding}{line}")
                else:
                    # Mensaje de una sola línea
                    visual_width = self._visual_len(text) + 3 + self._visual_len(time_and_tick)
                    padding = " " * max(0, PAD_WIDTH - visual_width)
                    lines.append(f"{padding}{text}   <msg-sent>{time_and_tick}</msg-sent>")
        return "\n".join(lines)

    def refresh_ui(self):
        from prompt_toolkit.formatted_text import HTML
        chat_content = self._get_chat_content()
        self._chat_control.text = HTML(chat_content)
        
        lines = []
        for k in self.contact_keys:
            info = self.db.get_contact_info(k)
            if not info:
                continue
            icon = "🟢" if info.get("is_connected") else ("🟡" if info.get("ip") else "🔴")
            prefix = "➤ " if k == self.current_cn else "  "
            full_name = info.get("name", k)
            name_parts = full_name.split()
            if len(name_parts) >= 2:
                if "," in full_name:
                    parts = full_name.split(",")
                    apellidos = parts[0].strip().split()
                    nombre = parts[1].strip().split()[0] if len(parts) > 1 else ""
                    display_name = f"{nombre} {apellidos[0]}"
                else:
                    display_name = f"{name_parts[0]} {name_parts[1]}"
            else:
                display_name = full_name
            if ":" in k:
                port = k.split(":")[1]
                display_name = f"{display_name} [:{port}]"
            unread = self.db.get_unread_count(k, self.my_nick)
            if unread > 0:
                lines.append(f"{prefix}{icon} {display_name} 🔔({unread})")
            else:
                lines.append(f"{prefix}{icon} {display_name}")
        
        self.w_contacts.text = "\n".join(lines)
        self.app.invalidate()

    def move_selection(self, delta):
        if not self.contact_keys:
            return
        idx = self.contact_keys.index(self.current_cn) if self.current_cn in self.contact_keys else 0
        new_idx = (idx + delta) % len(self.contact_keys)
        self.current_cn = self.contact_keys[new_idx]
        self.db.mark_messages_as_read(self.current_cn, self.my_nick)
        self.refresh_ui()

    def add_peer(self, name, ip, port):
        existing_cn = None
        all_contacts = self.db.get_all_contacts()
        for cn, info in all_contacts.items():
            if info.get("ip") == ip and info.get("port") == port:
                existing_cn = cn
                break
        if not existing_cn:
            for cn, info in all_contacts.items():
                if info.get("name") == name:
                    existing_cn = cn
                    break
        if existing_cn:
            self.db.add_or_update_contact(existing_cn, name=name, ip=ip, port=port)
        else:
            contact_id = f"{ip}:{port}"
            self.db.add_or_update_contact(contact_id, name=name, ip=ip, port=port)
            if contact_id not in self.contact_keys:
                self.contact_keys.append(contact_id)
                self.contact_keys.sort()
            if not self.current_cn:
                self.current_cn = contact_id
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, real_cn, msg_id=None):
        if text == "SESSIONS_READY":
            asyncio.create_task(self._auto_connect_and_send_all())
            return
        
        matching_contacts = []
        all_contacts = self.db.get_all_contacts()
        
        for cn, info in all_contacts.items():
            if (info.get("ip") == addr[0] and info.get("port") == addr[1]) or info.get("name") == real_cn:
                matching_contacts.append(cn)
        
        if len(matching_contacts) > 1:
            contact_id = matching_contacts[0]
            for dup_cn in matching_contacts[1:]:
                if dup_cn in self.contact_keys:
                    self.contact_keys.remove(dup_cn)
                if dup_cn in self.pending_handshakes:
                    self.pending_handshakes.remove(dup_cn)
                dup_info = self.db.get_contact_info(dup_cn)
                if dup_info and dup_info.get("msgs"):
                    main_info = self.db.get_contact_info(contact_id)
                    if main_info:
                        main_info["msgs"].extend(dup_info["msgs"])
                if dup_cn in self.db.data["contacts"]:
                    del self.db.data["contacts"][dup_cn]
            self.db.save()
        elif len(matching_contacts) == 1:
            contact_id = matching_contacts[0]
        else:
            contact_id = f"{addr[0]}:{addr[1]}"
        
        if contact_id in self.pending_handshakes:
            self.pending_handshakes.remove(contact_id)
        
        self.db.add_or_update_contact(contact_id, name=real_cn, ip=addr[0], port=addr[1])
        if contact_id not in self.contact_keys:
            self.contact_keys.append(contact_id)
            self.contact_keys.sort()

        ts = datetime.now().strftime("%H:%M")
        
        if text == "HANDSHAKE_OK":
            self.db.set_contact_connected(contact_id, True)
            msgs = self.db.get_history(contact_id)
            user_msgs = [m for m in msgs if m.get('sender') != "Sys"]
            if len(user_msgs) == 0:
                self.db.add_message(contact_id, "Sys", "🔒 Conexión segura establecida", "system", ts)
            self.check_pending_messages(contact_id, addr[0], addr[1])
        elif text == "SESSION_RESTORED":
            self.db.set_contact_connected(contact_id, True)
            self.check_pending_messages(contact_id, addr[0], addr[1])
        elif text == "PEER_RECONNECTED":
            self.db.set_contact_connected(contact_id, True)
            self.check_pending_messages(contact_id, addr[0], addr[1])
        elif text.startswith("HANDSHAKE_ERROR"):
            self.db.set_contact_connected(contact_id, False)
        elif text == "ERROR_DESCIFRADO":
            pass
        elif text.startswith("ACK|"):
            ack_msg_id = text.split('|', 1)[1]
            self.db.mark_message_status(contact_id, ack_msg_id, "delivered")
        else:
            self.db.set_contact_connected(contact_id, True)
            received_msg_id = self.db.add_message(contact_id, real_cn, text, "received", ts, msg_id=msg_id)
            if self.current_cn == contact_id:
                self.db.mark_message_as_read_by_id(contact_id, received_msg_id)
        
        self.refresh_ui()

    def check_pending_messages(self, cn, ip, port):
        pending = self.db.get_pending_messages(cn)
        if not pending:
            return
        
        for msg in pending:
            if self.protocol.enviar_mensaje(ip, port, msg['text'], msg['id']):
                self.db.mark_message_status(cn, msg['id'], "sent")
            else:
                info = self.db.get_contact_info(cn)
                contact_name = info.get("name", cn) if info else cn
                self.protocol.enviar_handshake(ip, port, cn=contact_name)
                break
        
        self.refresh_ui()

    async def handle_enter(self):
        # Detectar desde qué cuadro se envía
        if self.app.layout.has_focus(self.w_ascii):
            # Enviar desde ASCII
            ascii_key = self.w_ascii.text.strip()
            self.w_ascii.text = ""
            
            if ascii_key in self.ascii_art:
                # Encontrado en el JSON, enviar el arte ASCII
                ascii_text = self.ascii_art[ascii_key]
                # Usar el mismo flujo que w_input pero con el arte ASCII
                if not self.current_cn:
                    return
                info = self.db.get_contact_info(self.current_cn)
                if not info:
                    return
                ip, port = info.get("ip"), info.get("port")
                ts = datetime.now().strftime("%H:%M")
                
                if not ip or not self.protocol.tiene_sesion(ip, port):
                    self.db.add_message(self.current_cn, self.my_nick, ascii_text, "pending", ts)
                    self.refresh_ui()
                    return
                
                msg_id = self.db.add_message(self.current_cn, self.my_nick, ascii_text, "sent", ts)
                if not self.protocol.enviar_mensaje(ip, port, ascii_text, msg_id):
                    self.db.mark_message_status(self.current_cn, msg_id, "pending")
                    self.db.set_contact_connected(self.current_cn, False)
                    self.protocol.cerrar_sesion(ip, port)
                self.refresh_ui()
            return
        
        # Enviar desde w_input normal
        text = self.w_input.text.strip()
        if not self.current_cn:
            self.w_input.text = ""
            return
        info = self.db.get_contact_info(self.current_cn)
        if not info:
            return
        ip, port = info.get("ip"), info.get("port")
        ts = datetime.now().strftime("%H:%M")
        if not ip:
            if text:
                self.db.add_message(self.current_cn, "Sys", "Usuario Offline - Sin IP", "error", ts)
            self.w_input.text = ""
            self.refresh_ui()
            return
        if not self.protocol.tiene_sesion(ip, port):
            if text:
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", ts)
                self.w_input.text = ""
            if self.current_cn not in self.pending_handshakes:
                contact_name = info.get("name", self.current_cn)
                self.protocol.enviar_handshake(ip, port, cn=contact_name)
                self.pending_handshakes.add(self.current_cn)
                self.refresh_ui()
            return
        if text:
            msg_id = self.db.add_message(self.current_cn, self.my_nick, text, "sent", ts)
            self.w_input.text = ""
            if not self.protocol.enviar_mensaje(ip, port, text, msg_id):
                self.db.mark_message_status(self.current_cn, msg_id, "pending")
                self.db.set_contact_connected(self.current_cn, False)
                self.protocol.cerrar_sesion(ip, port)
            self.refresh_ui()

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

    async def _auto_connect_and_send_all(self):
        await asyncio.sleep(0.5)
        all_contacts = self.db.get_all_contacts()
        
        for cn, info in all_contacts.items():
            ip = info.get("ip")
            port = info.get("port")
            
            if not ip or not port:
                continue
            
            original_cn = self.current_cn
            self.current_cn = cn
            await self.handle_enter()
            self.current_cn = original_cn
            
            await asyncio.sleep(0.2)
            
            if self.protocol.tiene_sesion(ip, port):
                pending = self.db.get_pending_messages(cn)
                if pending:
                    for msg in pending:
                        if self.protocol.enviar_mensaje(ip, port, msg['text'], msg['id']):
                            self.db.mark_message_status(cn, msg['id'], "sent")
        
        self.refresh_ui()

    async def _retry_pending_messages(self):
        await asyncio.sleep(2)
        
        while True:
            await asyncio.sleep(3)
            
            for cn in list(self.contact_keys):
                info = self.db.get_contact_info(cn)
                if not info:
                    continue
                    
                ip = info.get("ip")
                port = info.get("port")
                
                if not ip or not port:
                    continue
                
                pending = self.db.get_pending_messages(cn)
                if not pending:
                    continue
                
                if self.protocol.tiene_sesion(ip, port):
                    enviados = 0
                    for msg in pending:
                        if self.protocol.enviar_mensaje(ip, port, msg['text'], msg['id']):
                            self.db.mark_message_status(cn, msg['id'], "sent")
                            enviados += 1
                    
                    if enviados > 0:
                        self.db.set_contact_connected(cn, True)
                        self.refresh_ui()

    async def _check_ack_timeouts(self):
        while True:
            await asyncio.sleep(2)
            for cn in list(self.contact_keys):
                info = self.db.get_contact_info(cn)
                if info and info.get("is_connected"):
                    has_timeout = self.db.check_message_timeouts(cn, timeout_seconds=5)
                    if has_timeout:
                        msgs = self.db.get_history(cn)
                        for msg in msgs:
                            if msg.get("status") == "sent":
                                self.db.mark_message_status(cn, msg["id"], "pending")
                        self.refresh_ui()

    async def run(self):
        self._timeout_check_task = asyncio.create_task(self._check_ack_timeouts())
        self._retry_task = asyncio.create_task(self._retry_pending_messages())
        
        try:
            await self.app.run_async()
        finally:
            if self._timeout_check_task:
                self._timeout_check_task.cancel()
                try:
                    await self._timeout_check_task
                except asyncio.CancelledError:
                    pass
            if self._retry_task:
                self._retry_task.cancel()
                try:
                    await self._retry_task
                except asyncio.CancelledError:
                    pass
