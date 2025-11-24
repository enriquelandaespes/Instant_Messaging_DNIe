import asyncio
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

        self.w_contacts = TextArea(focusable=False, width=35)
        self.w_chat = TextArea(text="", multiline=True, focusable=False, scrollbar=True, read_only=True, wrap_lines=True)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        self.layout = Layout(
            HSplit([
                VSplit([
                    Frame(self.w_contacts, title="Vecinos (DNIe)"),
                    Frame(self.w_chat, title=self._get_chat_title)
                ]),
                Frame(self.w_input, title=f"Escribe aquí ({my_nick})")
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

        from prompt_toolkit.styles import Style
        custom_style = Style.from_dict({
            'date-separator': 'fg:ansigray italic',
            'time-small': 'fg:ansigray',
            'time-small-sent': 'fg:#666666',
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
                center_pad = " " * max(0, (PAD_WIDTH - len(text)) // 2)
                lines.append(f"{center_pad}--- {text} ---")
            elif status == 'received' or sender != self.my_nick:
                lines.append(f"[{formatted_time}] {sender}:")
                lines.append(f" > {text}")
            else:
                if status == 'delivered':
                    tick = "✓✓"
                elif status == 'sent':
                    tick = "✓"
                elif status == 'pending':
                    tick = "🕒"
                else:
                    tick = "🕒"
                time_and_tick = f"{formatted_time} {tick}"
                line_content = f"{text}   {time_and_tick}"
                padding = " " * max(0, PAD_WIDTH - len(line_content))
                lines.append(f"{padding}{text}   {time_and_tick}")
        return "\n".join(lines)

    def refresh_ui(self):
        chat_content = self._get_chat_content()
        self.w_chat.text = chat_content
        
        try:
            self.w_chat.buffer.cursor_position = len(chat_content)
            self.w_chat.buffer.cursor_down(count=999999)
        except:
            pass
        
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
        already_marked = set()
        
        while True:
            await asyncio.sleep(2)
            for cn in list(self.contact_keys):
                info = self.db.get_contact_info(cn)
                if info and info.get("is_connected"):
                    has_timeout = self.db.check_message_timeouts(cn, timeout_seconds=5)
                    if has_timeout:
                        msgs = self.db.get_history(cn)
                        changed = False
                        for msg in msgs:
                            msg_id = msg.get("id")
                            if msg.get("status") == "sent" and msg_id not in already_marked:
                                self.db.mark_message_status(cn, msg_id, "pending")
                                already_marked.add(msg_id)
                                changed = True
                        
                        if changed:
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
