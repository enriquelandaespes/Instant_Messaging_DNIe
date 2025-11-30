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

class ChatTUI:
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
        self._reconnect_timeout_task = None
        self._window_monitor_task = None
        self._ui_refresh_task = None  # Nueva tarea para forzar redibujado
        self.sending_pending = set()
        self.pending_sent = {}
        
        self._last_line_count = 0
        self.scroll_offset = 0
        self._last_window_width = 0
        
        self.ascii_art = {}
        try:
            ascii_path = os.path.join(os.path.dirname(__file__), 'ascii.json') # Archivo para el ASCII
            with open(ascii_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.ascii_art = data.get('ascii', {})
        except Exception as e:
            print(f"Error cargando ascii.json: {e}")
        
        self.w_contacts = TextArea(focusable=False, width=35)
        
        self.chat_control = FormattedTextControl( # Obtenemos el contenido del chat
            text=self.get_chat_content,
            get_cursor_position=self.get_safe_cursor_position,
            focusable=False,
            show_cursor=False
        )
        
        self.w_chat_window = Window( # Ventana
            content=self.chat_control,
            wrap_lines=True,
            always_hide_cursor=True,
            style="class:chat-bg",
            allow_scroll_beyond_bottom=False,
            dont_extend_height=False
        )
        
        self.w_ascii = TextArea(height=2, prompt="> ", multiline=True, width=35)
        self.w_input = TextArea(height=2, prompt="> ", multiline=True)
        self.w_suggestions = TextArea(focusable=False, height=1, style="fg:ansigray")
        
        def on_ascii_text_changed(_):
            self.update_ascii_suggestions() # Para ayudar a poner el ASCII
        self.w_ascii.buffer.on_text_changed += on_ascii_text_changed

        self.layout = Layout(
            HSplit([
                VSplit([
                    Frame(self.w_contacts, title="👥 Contactos"),
                    Frame(self.w_chat_window, title=self.get_chat_title)
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

        style = Style.from_dict({ # Estilo de las diferetes elemenos de la TUI
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
        self.load_initial_contacts()

    def get_safe_cursor_position(self): # Obtenemos la posición del cursor(Scrolleo)
        if self._last_line_count <= 1:
            return Point(0, 0)
        max_offset = max(0, self._last_line_count - 1)
        actual_offset = min(self.scroll_offset, max_offset)
        target_line = max(0, self._last_line_count - 1 - actual_offset)
        return Point(x=0, y=target_line)

    def load_initial_contacts(self): # Carha de contactos que ya están en la base de datos
        for cn in self.db.get_all_contacts().keys():
            if cn not in self.contact_keys: 
                self.contact_keys.append(cn)
        self.contact_keys.sort()
        if self.current_cn is None:
            self.current_cn = "__AYUDA__"
        self.refresh_ui()

    def get_chat_title(self): # Obtenemos la parte de los chats en la tui
        if not self.current_cn: 
            return "Chat Seguro"
        if self.current_cn == "__AYUDA__":
            return "❓ Ayuda - Atajos de Teclado"
        elif self.current_cn == "__MI_CUENTA__":
            return "👤 Mi Cuenta"
        
        info = self.db.get_contact_info(self.current_cn)
        status = "🔴 DESCONECTADO"
        if info:
            if info.get("is_connected"):
                status = "🟢 CONECTADO"
            elif info.get("session_key"):
                status = "🔴 DESCONECTADO"
            else:
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

    def get_chat_content(self): # Obtenemos el contenido del chat para rellenarlo en la TUI
        if not self.current_cn:
            self._last_line_count = 1
            return [("class:msg-sys", "Esperando contactos...")]
        
        
        if self.current_cn == "__AYUDA__":
            return self.get_help_content()
        elif self.current_cn == "__MI_CUENTA__":
            return self.get_my_account_content()
        
        msgs = list(self.db.get_history(self.current_cn))
        formatted_lines = []
        
        # Margen superior interno
        formatted_lines.append(("", "\n"))
        
        # Obtener el ancho real de la ventana de chat
        try:
            PAD_WIDTH = self.w_chat_window.render_info.window_width if self.w_chat_window.render_info else 80
        except:
            PAD_WIDTH = 80
        
        # Reducir el ancho efectivo para dejar márgenes laterales (2 espacios a cada lado)
        PAD_WIDTH = max(40, PAD_WIDTH - 4)
        
        current_lines = 1  # Contamos la línea del margen superior
        # Rellenamos los mensajes por personas y el contenido del chat
        for m in msgs:
            sender = m.get('sender')
            text = m.get('text')
            timestamp_str = m.get('timestamp')
            status = m.get('status', '')
            
            if timestamp_str:
                try:
                    dt = datetime.fromisoformat(timestamp_str)
                    time = dt.strftime("%H:%M")
                    full_date = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    time = timestamp_str if len(timestamp_str) <= 5 else "??:??"
                    full_date = None
            else:
                time = "??:??"
                full_date = None
            
            formatted_time = self.format_timestamp(time, full_date)
            
            formatted_lines.append(("", "\n"))
            current_lines += 1
            
            MARGIN = "  "  # Margen izquierdo de 2 espacios
            
            if sender == "Sys":
                center_pad = " " * max(0, (PAD_WIDTH - self.visual_len(text)) // 2)
                formatted_lines.append(("class:msg-sys", f"{MARGIN}{center_pad}--- {text} ---"))
                
            elif status == 'received' or sender != self.my_nick:
                formatted_lines.append(("class:msg-recv", f"{MARGIN}[{formatted_time}] {sender}:\n"))
                current_lines += 1
                for line in text.split('\n'):
                    formatted_lines.append(("class:msg-recv", f"{MARGIN} > {line}\n"))
                    current_lines += 1
                    
            else:
                if status == 'delivered': 
                    tick = "✅"
                elif status == 'sent': 
                    tick = "🕒"
                else: 
                    tick = "🕒"
                
                text_lines = text.split('\n')
                
                # Calcular el ancho del timestamp con tick
                time_info = f"{formatted_time} {tick}"
                time_width = self.visual_len(time_info)
                
                if len(text_lines) > 1:
                    # Múltiples líneas
                    for i, line in enumerate(text_lines):
                        line_width = self.visual_len(line)
                        if i == len(text_lines) - 1:
                            # Última línea: verificar si cabe con el timestamp
                            if line_width + time_width + 3 <= PAD_WIDTH:
                                # Cabe en la misma línea
                                padding = " " * max(0, PAD_WIDTH - line_width - time_width - 3)
                                formatted_lines.append(("", MARGIN + padding))
                                formatted_lines.append(("class:msg-sent", line))
                                formatted_lines.append(("class:time", f"   {formatted_time} "))
                                tick_style = "class:tick-read" if status == 'delivered' else "class:tick-sent"
                                formatted_lines.append((tick_style, f"{tick}\n"))
                                current_lines += 1
                            else:
                                # No cabe: línea aparte para el timestamp
                                padding = " " * max(0, PAD_WIDTH - line_width)
                                formatted_lines.append(("", MARGIN + padding + line + "\n"))
                                current_lines += 1
                                # Timestamp en nueva línea a la derecha
                                time_padding = " " * max(0, PAD_WIDTH - time_width)
                                formatted_lines.append(("", MARGIN + time_padding))
                                formatted_lines.append(("class:time", f"{formatted_time} "))
                                tick_style = "class:tick-read" if status == 'delivered' else "class:tick-sent"
                                formatted_lines.append((tick_style, f"{tick}\n"))
                                current_lines += 1
                        else:
                            # Líneas intermedias
                            padding = " " * max(0, PAD_WIDTH - line_width)
                            formatted_lines.append(("", MARGIN + padding + line + "\n"))
                            current_lines += 1
                else:
                    # Una sola línea
                    text_width = self.visual_len(text)
                    if text_width + time_width + 3 <= PAD_WIDTH:
                        # Cabe todo en una línea
                        padding = " " * max(0, PAD_WIDTH - text_width - time_width - 3)
                        formatted_lines.append(("", MARGIN + padding))
                        formatted_lines.append(("class:msg-sent", text))
                        formatted_lines.append(("class:time", f"   {formatted_time} "))
                        tick_style = "class:tick-read" if status == 'delivered' else "class:tick-sent"
                        formatted_lines.append((tick_style, f"{tick}\n"))
                        current_lines += 1
                    else:
                        # Mensaje muy largo: timestamp en línea separada
                        padding = " " * max(0, PAD_WIDTH - text_width)
                        formatted_lines.append(("", MARGIN + padding))
                        formatted_lines.append(("class:msg-sent", text + "\n"))
                        current_lines += 1
                        # Timestamp en nueva línea a la derecha
                        time_padding = " " * max(0, PAD_WIDTH - time_width)
                        formatted_lines.append(("", MARGIN + time_padding))
                        formatted_lines.append(("class:time", f"{formatted_time} "))
                        tick_style = "class:tick-read" if status == 'delivered' else "class:tick-sent"
                        formatted_lines.append((tick_style, f"{tick}\n"))
                        current_lines += 1
        
        # Margen inferior interno
        formatted_lines.append(("", "\n"))
        current_lines += 1
        
        self._last_line_count = current_lines
        return formatted_lines
    
    def get_my_account_content(self): # Contenido de la pestaña de mi cuenta
        formatted_lines = []
        formatted_lines.append(("", "\n\n"))
        formatted_lines.append(("class:msg-sys", "╔════════════════════════════════════════════════════════════════════════╗\n"))
        formatted_lines.append(("class:msg-sys", "║                         📄 MI CUENTA                                   ║\n"))
        formatted_lines.append(("class:msg-sys", "╚════════════════════════════════════════════════════════════════════════╝\n"))
        formatted_lines.append(("", "\n"))
        formatted_lines.append(("class:msg-recv", "👤 Usuario:\n"))
        formatted_lines.append(("class:msg-sent", f"   {self.my_nick}\n"))
        formatted_lines.append(("", "\n"))
        formatted_lines.append(("class:msg-recv", "🌐 Dirección IP:\n"))
        formatted_lines.append(("class:msg-sent", f"   {self.my_ip}\n"))
        formatted_lines.append(("", "\n"))
        formatted_lines.append(("class:msg-recv", "🔌 Puerto UDP:\n"))
        formatted_lines.append(("class:msg-sent", f"   {self.my_port}\n"))
        formatted_lines.append(("", "\n"))
        has_connected = any(self.db.get_contact_info(k).get("is_connected", False) for k in self.contact_keys if self.db.get_contact_info(k))
        status_icon = "🟢" if has_connected else "🟡"
        status_text = "En línea" if has_connected else "Disponible"
        formatted_lines.append(("class:msg-recv", "📊 Estado:\n"))
        formatted_lines.append(("class:msg-sent", f"   {status_icon} {status_text}\n"))
        formatted_lines.append(("", "\n"))
        formatted_lines.append(("class:msg-recv", "👥 Contactos:\n"))
        formatted_lines.append(("class:msg-sent", f"   {len(self.contact_keys)} contacto(s)\n"))
        formatted_lines.append(("", "\n\n"))
        self._last_line_count = len(formatted_lines)
        return formatted_lines
    
    def get_help_content(self): # Contenido de la pestaña de ayuda  
        formatted_lines = []
        formatted_lines.append(("", "\n\n"))
        formatted_lines.append(("class:msg-sys", "╔════════════════════════════════════════════════════════════════════════╗\n"))
        formatted_lines.append(("class:msg-sys", "║                      ❓ AYUDA - ATAJOS DE TECLADO                      ║\n"))
        formatted_lines.append(("class:msg-sys", "╚════════════════════════════════════════════════════════════════════════╝\n"))
        formatted_lines.append(("", "\n\n"))
        formatted_lines.append(("class:msg-recv", "🔍 NAVEGACIÓN:\n"))
        formatted_lines.append(("class:msg-sent", "   ↑ / ↓         Cambiar entre contactos\n"))
        formatted_lines.append(("class:msg-sent", "   Tab           Alternar entre campo Chat y ASCII\n"))
        formatted_lines.append(("", "\n"))
        formatted_lines.append(("class:msg-recv", "📜 SCROLL DEL CHAT:\n"))
        formatted_lines.append(("class:msg-sent", "   Shift + ↑     Subir en el historial (5 líneas)\n"))
        formatted_lines.append(("class:msg-sent", "   Shift + ↓     Bajar en el historial (5 líneas)\n"))
        formatted_lines.append(("class:msg-sys", "   * El scroll se mantiene hasta que envíes un mensaje\n"))
        formatted_lines.append(("", "\n"))
        formatted_lines.append(("class:msg-recv", "💬 MENSAJES:\n"))
        formatted_lines.append(("class:msg-sent", "   Enter         Enviar mensaje o conectar con usuario\n"))
        formatted_lines.append(("class:msg-sys", "   * Si no hay sesión, se restablece automáticamente\n"))
        formatted_lines.append(("", "\n"))
        formatted_lines.append(("class:msg-recv", "🎨 ASCII ART:\n"))
        formatted_lines.append(("class:msg-sent", "   1. Presiona Tab para cambiar al campo ASCII\n"))
        formatted_lines.append(("class:msg-sent", "   2. Escribe parte del nombre (ej: 'rifle')\n"))
        formatted_lines.append(("class:msg-sent", "   3. Aparecerán sugerencias debajo\n"))
        formatted_lines.append(("class:msg-sent", "   4. Presiona Enter para enviar\n"))
        formatted_lines.append(("", "\n"))
        formatted_lines.append(("class:msg-recv", "🔌 CONEXIÓN:\n"))
        formatted_lines.append(("class:msg-sent", "   Ctrl + D      Desconectar del usuario actual\n"))
        formatted_lines.append(("class:msg-sent", "   Ctrl + C      Salir de la aplicación\n"))
        formatted_lines.append(("", "\n\n"))
        self._last_line_count = len(formatted_lines)
        return formatted_lines

    def format_timestamp(self, time_str, full_date_str=None): # Formato del timestamp de los mensajes
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

    def visual_len(self, text): # Longitud visual de los mensajes
        width = 0
        for char in text:
            ea = unicodedata.east_asian_width(char)
            if ea in ('F', 'W'):
                width += 2
            elif ea in ('Na', 'H', 'N', 'A'):
                width += 1
            else:
                width += 1
        return width

    def update_ascii_suggestions(self): # Sugerencias de ASCII
        current_text = self.w_ascii.text.strip().lower()
        if not current_text:
            self.w_suggestions.text = ""
            return
        matches = [key for key in self.ascii_art.keys() if current_text in key.lower()]
        if matches:
            suggestions_text = "  Sugerencias: " + ", ".join(matches[:5])
            if len(matches) > 5:
                suggestions_text += f" ... (+{len(matches)-5} más)"
            self.w_suggestions.text = suggestions_text
        else:
            self.w_suggestions.text = "  Sin coincidencias"

    def refresh_ui(self): # Refresca la UI
        lines = []
        special_contacts = [ "__AYUDA__", "__MI_CUENTA__"]
        
        for special in special_contacts:
            if special == "__AYUDA__":
                icon = "❓"
                display = "Ayuda"
            elif special == "__MI_CUENTA__":
                icon = "👤"
                display = "Mi Cuenta"
            prefix = "➞ " if self.current_cn == special else "  "
            lines.append(f"{prefix}{icon} {display}")
        
        if self.contact_keys: # Visual
            lines.append("")
            lines.append("─" * 32)
            lines.append("")
        
        for k in self.contact_keys: #Información del contacto
            info = self.db.get_contact_info(k)
            if not info:
                continue
            
            if info.get("is_connected"):
                icon = "🟢"
            elif info.get("session_key"):
                icon = "🔴"
            else:
                icon = "🟡"
            
            prefix = "➞ " if k == self.current_cn else "  " # Ponemos una flecha que indique en que pestaña estamos 
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

    def move_selection(self, delta): # Movimiento de la selección
        all_items = ["__AYUDA__", "__MI_CUENTA__"] + self.contact_keys
        if not all_items:
            return
        idx = all_items.index(self.current_cn) if self.current_cn in all_items else 0
        new_idx = (idx + delta) % len(all_items)
        self.current_cn = all_items[new_idx]
        self.scroll_offset = 0
        if self.current_cn not in ["__AYUDA__", "__MI_CUENTA__"] :
            self.db.mark_messages_as_read(self.current_cn, self.my_nick)
        self.refresh_ui()

    def add_peer(self, name, ip, port): # Añade un contacto
        existing_cn = None
        all_contacts = self.db.get_all_contacts()
        
        for cn, info in all_contacts.items():
            if info.get("ip") == ip and info.get("port") == port:
                existing_cn = cn
                break
        
        if not existing_cn:
            target_name = name.strip().lower()
            for cn, info in all_contacts.items():
                db_name = info.get("name", "").strip().lower()
                if db_name == target_name:
                    existing_cn = cn
                    break
        
        if existing_cn:
            contact_id = existing_cn
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

    def on_protocol_msg(self, addr, text, real_cn, msg_id=None): # Maneja los mensajes del protocolo
        if text == "SESSIONS_READY":
            asyncio.create_task(self.auto_connect_and_send_all())
            return
        
        if addr is None:
            return
        
        contact_id = None
        all_contacts = self.db.get_all_contacts()
        
        for cn, info in all_contacts.items():
            if info.get("ip") == addr[0] and info.get("port") == addr[1]:
                contact_id = cn
                break
        
        if not contact_id:
            for cn, info in all_contacts.items():
                if info.get("name") == real_cn:
                    contact_id = cn
                    break
        
        if not contact_id:
            contact_id = real_cn
        
        if contact_id in self.pending_handshakes:
            self.pending_handshakes.discard(contact_id)
        
        if contact_id in all_contacts:
            self.db.add_or_update_contact(contact_id, ip=addr[0], port=addr[1])
        else:
            self.db.add_or_update_contact(contact_id, name=real_cn, ip=addr[0], port=addr[1])
        
        if contact_id not in self.contact_keys:
            self.contact_keys.append(contact_id)
            self.contact_keys.sort()
        
        ts = datetime.now().strftime("%H:%M")
        
        if text in ["HANDSHAKE_OK_INIT", "HANDSHAKE_OK_RESP"]:
            self.db.set_contact_connected(contact_id, True)
            msgs = self.db.get_history(contact_id)
            user_msgs = [m for m in msgs if m.get('sender') != "Sys"]
            if len(user_msgs) == 0:
                self.db.add_message(contact_id, "Sys", "🔒 Conexión segura establecida", "system", ts)
            # Siempre empieza el handshake enviando pendientes
            self.protocol.enviar_pending_send(addr[0], addr[1])
            self.send_pending_messages(contact_id, addr[0], addr[1], lambda: self.protocol.enviar_pending_done(addr[0], addr[1]))
            self.pending_sent[(addr[0], addr[1])] = True
        
        elif text == "SESSION_RESTORED_INIT":
            self.db.set_contact_connected(contact_id, True)
            self.protocol.enviar_pending_send(addr[0], addr[1])
            self.send_pending_messages(contact_id, addr[0], addr[1], lambda: self.protocol.enviar_pending_done(addr[0], addr[1]))
            self.pending_sent[(addr[0], addr[1])] = True
        
        elif text == "SESSION_RESTORED_RESP":
            self.db.set_contact_connected(contact_id, True)
            self.pending_sent[(addr[0], addr[1])] = False
        
        elif text == "PEER_SENDING_PENDING":
            pass
        
        elif text == "SEND_MY_PENDING":
            if not self.pending_sent.get((addr[0], addr[1]), False):
                self.pending_sent[(addr[0], addr[1])] = True
                self.protocol.enviar_pending_send(addr[0], addr[1])
                self.send_pending_messages(contact_id, addr[0], addr[1], lambda: self.protocol.enviar_pending_done(addr[0], addr[1]))
        
        elif text == "RECONNECT_TIMEOUT":
            self.db.set_contact_connected(contact_id, False)
            self.refresh_ui()
            return
        
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

    def send_pending_messages(self, cn, ip, port, callback=None):
        # Envía todos los mensajes pendientes de la base de datos de un contacto
        pending = self.db.get_pending_messages(cn)
        if not pending or cn in self.sending_pending:
            if callback:
                callback()
            return
        
        if not self.protocol.tiene_sesion(ip, port):
            return
        
        self.sending_pending.add(cn)
        
        async def send_all_async():
            # Envía todos los mensajes pendientes de forma asincrona para evitar bloqueos
            for msg in pending:
                # Intentar enviar hasta 3 veces
                sent = False
                for _ in range(3):
                    if self.protocol.enviar_mensaje(ip, port, msg['text'], msg['id']):
                        self.db.mark_message_status(cn, msg['id'], "sent")
                        sent = True
                        break
                    await asyncio.sleep(0.1)
                
                if sent:
                    # Delay mayor para asegurar que el SO procese el paquete y evitar saturación
                    # Windows puede descartar paquetes si enviamos muy rápido
                    await asyncio.sleep(0.5)
                    self.refresh_ui()
                    if self.app:
                        self.app.invalidate()
            
            self.sending_pending.discard(cn)
            self.refresh_ui()
            if callback:
                callback()
        
        asyncio.create_task(send_all_async())

    async def handle_enter(self): # Maneja lo que ocurre tras un enter 
        if self.current_cn in ["__AYUDA__", "__MI_CUENTA__"]:
            return
        
        if self.app.layout.has_focus(self.w_ascii):
            ascii_key = self.w_ascii.text.strip()
            self.w_ascii.text = ""
            if ascii_key in self.ascii_art:
                ascii_text = self.ascii_art[ascii_key]
                if not self.current_cn:
                    return
                info = self.db.get_contact_info(self.current_cn)
                if not info:
                    return
                ip, port = info.get("ip"), info.get("port")
                ts = datetime.now().strftime("%H:%M")
                
                if not ip or not self.protocol.tiene_sesion(ip, port):
                    self.db.add_message(self.current_cn, self.my_nick, ascii_text, "pending", ts)
                    if ip and port:
                        self.protocol.enviar_handshake(ip, port, cn=self.current_cn)
                    self.refresh_ui()
                    return
                
                msg_id = self.db.add_message(self.current_cn, self.my_nick, ascii_text, "sent", ts)
                if not self.protocol.enviar_mensaje(ip, port, ascii_text, msg_id):
                    self.db.mark_message_status(self.current_cn, msg_id, "pending")
                    self.db.set_contact_connected(self.current_cn, False)
                    self.protocol.cerrar_sesion(ip, port)
                self.refresh_ui()
            return
        
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
                self.scroll_offset = 0
                self.w_input.text = ""
            if self.current_cn not in self.pending_handshakes:
                self.protocol.enviar_handshake(ip, port, cn=self.current_cn)
                self.pending_handshakes.add(self.current_cn)
                self.refresh_ui()
            return
        
        if text:
            msg_id = self.db.add_message(self.current_cn, self.my_nick, text, "sent", ts)
            self.scroll_offset = 0
            self.w_input.text = ""
            if not self.protocol.enviar_mensaje(ip, port, text, msg_id):
                self.db.mark_message_status(self.current_cn, msg_id, "pending")
                self.db.set_contact_connected(self.current_cn, False)
                self.protocol.cerrar_sesion(ip, port)
            self.refresh_ui()

    def force_disconnect(self): # Fuerza la desconexión manual de un peer(Como un bloqueo)
        if not self.current_cn:
            return
        info = self.db.get_contact_info(self.current_cn)
        if info and info.get("ip"):
            self.protocol.cerrar_sesion(info["ip"], info["port"])
            self.db.set_contact_connected(self.current_cn, False)
            ts = datetime.now().strftime("%H:%M")
            self.db.add_message(self.current_cn, "Sys", "Desconectado manualmente", "system", ts)
            self.refresh_ui()

    async def auto_connect_and_send_all(self):# Al conectarse maneja la lógica para avisar que se ha recoenctado
        await asyncio.sleep(0.5)
        all_contacts = list(self.db.get_all_contacts().items())
        
        for cn, info in all_contacts:
            ip = info.get("ip")
            port = info.get("port")
            if not ip or not port:
                continue
            
            self.protocol.enviar_handshake(ip, port, cn=cn)
            await asyncio.sleep(0.1)
        
        self.refresh_ui()

    async def monitor_window_size(self):
        """Monitorea cambios en el tamaño de la ventana y fuerza redibujado"""
        while True:
            await asyncio.sleep(0.1)  # Verificar cada 100ms
            try:
                if self.w_chat_window.render_info:
                    current_width = self.w_chat_window.render_info.window_width
                    if current_width != self._last_window_width and current_width > 0:
                        self._last_window_width = current_width
                        if self.app:
                            self.app.invalidate()
            except:
                pass
    
    async def force_ui_refresh(self):
        """Fuerza redibujado de la UI constantemente para evitar bloqueos"""
        while True:
            await asyncio.sleep(0.05)  # Cada 50ms
            try:
                if self.app:
                    self.app.invalidate()
            except:
                pass

    async def check_ack_timeouts(self): # Comprueba si se han acapado los timeout de los mensajes
        while True:
            await asyncio.sleep(0.5)
            for cn in list(self.contact_keys):
                if cn in self.sending_pending:
                    continue
                
                info = self.db.get_contact_info(cn)
                if info and info.get("is_connected"):
                    has_timeout = self.db.check_message_timeouts(cn, timeout_seconds=0.5)
                    if has_timeout:
                        self.db.set_contact_connected(cn, False)
                        ip = info.get("ip")
                        port = info.get("port")
                        if ip and port:
                            self.protocol.cerrar_sesion(ip, port)
                        msgs = self.db.get_history(cn)
                        for msg in msgs:
                            if msg.get("status") == "sent":
                                self.db.mark_message_status(cn, msg["id"], "pending")
                        self.refresh_ui()

    async def run(self):
        # Corre la TUI
        self._timeout_check_task = asyncio.create_task(self.check_ack_timeouts())
        self._reconnect_timeout_task = asyncio.create_task(self.protocol.check_reconnect_timeouts())
        self._window_monitor_task = asyncio.create_task(self.monitor_window_size())
        self._ui_refresh_task = asyncio.create_task(self.force_ui_refresh())  # Nueva tarea
        
        try:
            await self.app.run_async()
        finally:
            if self._timeout_check_task:
                self._timeout_check_task.cancel()
                try:
                    await self._timeout_check_task
                except asyncio.CancelledError:
                    pass
            if self._reconnect_timeout_task:
                self._reconnect_timeout_task.cancel()
                try:
                    await self._reconnect_timeout_task
                except asyncio.CancelledError:
                    pass
            if self._window_monitor_task:
                self._window_monitor_task.cancel()
                try:
                    await self._window_monitor_task
                except asyncio.CancelledError:
                    pass
            if self._ui_refresh_task:
                self._ui_refresh_task.cancel()
                try:
                    await self._ui_refresh_task
                except asyncio.CancelledError:
                    pass
