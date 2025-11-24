# gui.py
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
        self._timeout_check_task = None  # Tarea para verificar timeouts 

        # --- Widgets ---
        self.w_contacts = TextArea(focusable=False, width=35)
        
        # Usar TextArea para el chat - tiene scroll incorporado
        self.w_chat = TextArea(
            text="",
            multiline=True,
            focusable=False,
            scrollbar=True,
            read_only=True,
            wrap_lines=True
        )
        
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # --- Layout ---
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
        def _(event): self.force_disconnect()  # Ctrl+D para forzar desconexión (debug)

        # Estilos personalizados para texto
        from prompt_toolkit.styles import Style
        custom_style = Style.from_dict({
            'date-separator': 'fg:ansigray italic',  # Fecha separadora más discreta
            'time-small': 'fg:ansigray',  # Hora pequeña y gris para mensajes recibidos
            'time-small-sent': 'fg:#666666',  # Hora pequeña y gris oscuro para mensajes enviados
        })
        
        self.app = Application(
            layout=self.layout, 
            key_bindings=kb, 
            full_screen=True, 
            mouse_support=True,
            style=custom_style
        )
        self._load_initial_contacts()

    def _format_timestamp(self, time_str, full_date_str=None):
        """Convierte timestamp a formato legible: 'Hoy 17:59', 'Ayer 18:30', '21 Nov 10:15'"""
        try:
            if full_date_str:
                # Si tenemos fecha completa (formato: 'YYYY-MM-DD HH:MM')
                msg_datetime = datetime.strptime(full_date_str, "%Y-%m-%d %H:%M")
            else:
                # Solo tenemos hora, asumir hoy
                msg_datetime = datetime.strptime(f"{datetime.now().strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %H:%M")
            
            now = datetime.now()
            today = now.date()
            msg_date = msg_datetime.date()
            
            if msg_date == today:
                return f"Hoy {time_str}"
            elif msg_date == today - timedelta(days=1):
                return f"Ayer {time_str}"
            elif msg_date.year == today.year:
                # Mismo año: solo día y mes
                return msg_datetime.strftime(f"%d %b {time_str}")
            else:
                # Año diferente: fecha completa
                return msg_datetime.strftime(f"%d/%m/%y {time_str}")
        except:
            # Si falla el parsing, devolver el original
            return time_str

    def _load_initial_contacts(self):
        for cn in self.db.get_all_contacts().keys():
            if cn not in self.contact_keys: self.contact_keys.append(cn)
        self.contact_keys.sort()
        if self.contact_keys and self.current_cn is None:
            self.current_cn = self.contact_keys[0]
        self.refresh_ui()
        
        # Programar auto-conexión después de que la interfaz esté lista
        asyncio.create_task(self._auto_connect_saved_contacts())

    def _get_chat_title(self):
        if not self.current_cn: return "Chat Seguro"
        info = self.db.get_contact_info(self.current_cn)
        status = "🔴 OFFLINE"
        if info and info.get("is_connected"): status = "🟢 CONECTADO"
        elif info and info.get("ip"): status = "🟡 DISPONIBLE"
        if self.current_cn in self.pending_handshakes: status = "⏳ CONECTANDO..."
        
        # Mostrar nombre legible y acortado
        full_name = info.get("name", self.current_cn) if info else self.current_cn
        
        # Acortar nombre: solo nombre y primer apellido
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
        """Genera contenido del chat como texto plano"""
        if not self.current_cn:
            return "Esperando contactos..."
        
        msgs = list(self.db.get_history(self.current_cn))
        lines = []
        PAD_WIDTH = 80
        last_date = None

        for m in msgs:
            sender, text, timestamp_iso, status = m.get('sender'), m.get('text'), m.get('timestamp'), m.get('status', '')
            
            # Convertir timestamp ISO a HH:MM
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
            
            # Añadir separador de fecha si cambió el día
            formatted_time = self._format_timestamp(time, full_date)
            current_date = formatted_time.split()[0] if formatted_time and ' ' in formatted_time else None
            
            if current_date and last_date != current_date and current_date != time:
                if last_date is not None:
                    lines.append("")
                separator = f"- {current_date} -"
                center_pad = " " * max(0, (PAD_WIDTH - len(separator)) // 2)
                lines.append(f"{center_pad}{separator}")
                last_date = current_date
            
            lines.append("")  # Línea en blanco entre mensajes
            
            # Mensajes del sistema
            if sender == "Sys":
                center_pad = " " * max(0, (PAD_WIDTH - len(text)) // 2)
                lines.append(f"{center_pad}--- {text} ---")
            
            # Mensajes RECIBIDOS (del otro usuario) - SIN TICK, a la izquierda
            elif status == 'received' or sender != self.my_nick:
                lines.append(f"[{formatted_time}] {sender}:")
                lines.append(f" > {text}")
            
            # Mensajes ENVIADOS por mí - CON TICK, a la derecha
            else:
                if status == 'delivered': tick = "✓✓"
                elif status == 'sent': tick = "✓"
                else: tick = "🕒" # pending
                
                # Fecha/Hora y tick
                time_and_tick = f"{formatted_time} {tick}"
                
                # Calcular padding para alinear a la derecha
                line_content = f"{text}   {time_and_tick}"
                padding = " " * max(0, PAD_WIDTH - len(line_content))
                
                lines.append(f"{padding}{text}   {time_and_tick}")
        
        return "\n".join(lines)

    def refresh_ui(self):
        # Actualizar contenido del chat
        self.w_chat.text = self._get_chat_content()
        # Hacer scroll automático al final
        self.w_chat.buffer.cursor_position = len(self.w_chat.text)
        
        # Actualizar lista de contactos
        lines = []
        for k in self.contact_keys:
            info = self.db.get_contact_info(k)
            if not info: continue
            icon = "🟢" if info.get("is_connected") else ("🟡" if info.get("ip") else "🔴")
            prefix = "➤ " if k == self.current_cn else "  "
            
            # Mostrar nombre legible en lugar de IP:Puerto
            full_name = info.get("name", k)
            
            # Acortar nombre: solo nombre y primer apellido
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
            
            # Añadir campanita si hay mensajes no leídos
            unread = self.db.get_unread_count(k, self.my_nick)
            if unread > 0:
                lines.append(f"{prefix}{icon} {display_name} 🔔({unread})")
            else:
                lines.append(f"{prefix}{icon} {display_name}")
        
        self.w_contacts.text = "\n".join(lines)
        self.app.invalidate()

    def move_selection(self, delta):
        if not self.contact_keys: return
        idx = self.contact_keys.index(self.current_cn) if self.current_cn in self.contact_keys else 0
        new_idx = (idx + delta) % len(self.contact_keys)
        self.current_cn = self.contact_keys[new_idx]
        self.db.mark_messages_as_read(self.current_cn, self.my_nick)
        self.refresh_ui()

    def add_peer(self, name, ip, port):
        # Buscar si ya existe un contacto con este IP:Puerto O con este nombre
        existing_cn = None
        all_contacts = self.db.get_all_contacts()
        
        # Primero buscar por IP:Puerto
        for cn, info in all_contacts.items():
            if info.get("ip") == ip and info.get("port") == port:
                existing_cn = cn
                break
        
        # Si no se encontró, buscar por nombre (para evitar duplicados)
        if not existing_cn:
            for cn, info in all_contacts.items():
                if info.get("name") == name:
                    existing_cn = cn
                    break
        
        if existing_cn:
            # Ya existe, solo actualizar IP/puerto
            self.db.add_or_update_contact(existing_cn, name=name, ip=ip, port=port)
        else:
            # Nuevo contacto - usar IP:Puerto como ID
            contact_id = f"{ip}:{port}"
            self.db.add_or_update_contact(contact_id, name=name, ip=ip, port=port)
            if contact_id not in self.contact_keys:
                self.contact_keys.append(contact_id)
                self.contact_keys.sort()
            if not self.current_cn: 
                self.current_cn = contact_id
        
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, real_cn, msg_id=None):
        # Buscar TODOS los contactos que coincidan con este IP:Puerto O con este nombre
        matching_contacts = []
        all_contacts = self.db.get_all_contacts()
        
        for cn, info in all_contacts.items():
            if (info.get("ip") == addr[0] and info.get("port") == addr[1]) or info.get("name") == real_cn:
                matching_contacts.append(cn)
        
        # Si hay varios, fusionarlos en uno solo (el primero)
        if len(matching_contacts) > 1:
            contact_id = matching_contacts[0]
            # Eliminar los duplicados de la lista de keys
            for dup_cn in matching_contacts[1:]:
                if dup_cn in self.contact_keys:
                    self.contact_keys.remove(dup_cn)
                if dup_cn in self.pending_handshakes:
                    self.pending_handshakes.remove(dup_cn)
                # Fusionar en la DB (mover mensajes si los hay)
                dup_info = self.db.get_contact_info(dup_cn)
                if dup_info and dup_info.get("msgs"):
                    main_info = self.db.get_contact_info(contact_id)
                    if main_info:
                        main_info["msgs"].extend(dup_info["msgs"])
                # Eliminar duplicado de la DB
                if dup_cn in self.db.data["contacts"]:
                    del self.db.data["contacts"][dup_cn]
            self.db.save()
        elif len(matching_contacts) == 1:
            contact_id = matching_contacts[0]
        else:
            # No existe, crear uno nuevo con IP:Puerto como ID
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
            # Añadir mensaje de conexión segura solo si es la primera vez (no tiene mensajes de chat)
            msgs = self.db.get_history(contact_id)
            user_msgs = [m for m in msgs if m.get('sender') != "Sys"]
            if len(user_msgs) == 0:
                self.db.add_message(contact_id, "Sys", "🔒 Conexión segura establecida", "system", ts)
            self.check_pending_messages(contact_id, addr[0], addr[1])
        elif text == "SESSION_RESTORED":
            self.db.set_contact_connected(contact_id, True)
            self.check_pending_messages(contact_id, addr[0], addr[1])
        elif text.startswith("HANDSHAKE_ERROR"):
            self.db.set_contact_connected(contact_id, False)
        elif text == "ERROR_DESCIFRADO":
            pass
        elif text.startswith("ACK|"):
            msg_id = text.split('|', 1)[1]
            self.db.mark_message_status(contact_id, msg_id, "delivered")
        else:
            self.db.set_contact_connected(contact_id, True)
            # Pasar msg_id como remote_id para evitar duplicados
            msg_id = self.db.add_message(contact_id, real_cn, text, "received", ts, remote_id=msg_id)
            if self.current_cn == contact_id:
                self.db.mark_message_as_read_by_id(contact_id, msg_id)
        self.refresh_ui()

    def check_pending_messages(self, cn, ip, port):
        pending = self.db.get_pending_messages(cn)
        for msg in pending:
            if self.protocol.enviar_mensaje(ip, port, msg['text'], msg['id']):
                self.db.mark_message_status(cn, msg['id'], "sent")
            else:
                info = self.db.get_contact_info(cn)
                contact_name = info.get("name", cn) if info else cn
                self.protocol.enviar_handshake(ip, port, cn=contact_name)
        self.refresh_ui()

    async def handle_enter(self):
        text = self.w_input.text.strip()
        if not self.current_cn:
            self.w_input.text = ""
            return
            
        info = self.db.get_contact_info(self.current_cn)
        if not info: return 
        ip, port = info.get("ip"), info.get("port")
        ts = datetime.now().strftime("%H:%M")

        if not ip:
            if text:
                self.db.add_message(self.current_cn, "Sys", "Usuario Offline - Sin IP", "error", ts)
            self.w_input.text = ""
            self.refresh_ui()
            return

        # Si NO hay sesión guardada - hacer handshake manual
        if not self.protocol.tiene_sesion(ip, port):
            if text:
                # Guardar mensaje como pendiente
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", ts)
                self.w_input.text = ""
            
            # Iniciar handshake
            if self.current_cn not in self.pending_handshakes:
                contact_name = info.get("name", self.current_cn)
                self.protocol.enviar_handshake(ip, port, cn=contact_name)
                self.pending_handshakes.add(self.current_cn)
                # No mostrar mensaje innecesario
                self.refresh_ui()

        # Si HAY sesión y hay texto - enviar mensaje
        if text:
            msg_id = self.db.add_message(self.current_cn, self.my_nick, text, "sent", ts)
            self.w_input.text = ""
            
            if not self.protocol.enviar_mensaje(ip, port, text, msg_id):
                # Fallo al enviar - marcar como pendiente
                self.db.mark_message_status(self.current_cn, msg_id, "pending")
                self.db.set_contact_connected(self.current_cn, False)
                self.protocol.cerrar_sesion(ip, port)
                
            self.refresh_ui()

    def force_disconnect(self):
        """Forzar desconexión del contacto actual (para pruebas)"""
        if not self.current_cn: return
        info = self.db.get_contact_info(self.current_cn)
        if info and info.get("ip"):
            self.protocol.cerrar_sesion(info["ip"], info["port"])
            self.db.set_contact_connected(self.current_cn, False)
            ts = datetime.now().strftime("%H:%M")
            self.db.add_message(self.current_cn, "Sys", "Desconectado manualmente", "system", ts)
            self.refresh_ui()

    async def _send_pending_silent(self, cn, ip, port):
        """Envía mensajes pendientes silenciosamente en background"""
        await asyncio.sleep(0.5)  # Pequeño delay
        pending = self.db.get_pending_messages(cn)
        for msg in pending:
            if self.protocol.enviar_mensaje(ip, port, msg['text'], msg['id']):
                self.db.mark_message_status(cn, msg['id'], "sent")
            else:
                break  # Si falla, salir
        self.refresh_ui()
    async def _auto_connect_saved_contacts(self):
        """Intenta conectar automáticamente con todos los contactos que tienen sesión guardada"""
        # Esperar un momento a que la interfaz esté lista
        await asyncio.sleep(1)
        
        all_contacts = self.db.get_all_contacts()
        for cn, info in all_contacts.items():
            ip = info.get("ip")
            port = info.get("port")
            name = info.get("name", cn)
            
            # Solo intentar conectar si tiene IP y puerto
            if not ip or not port:
                continue
            
            # Iniciar handshake activo con todos los contactos conocidos
            # Esto verificará si están online y enviará mensajes pendientes
            self.protocol.enviar_handshake(ip, port, cn=name)
        
        self.refresh_ui()
    
    async def _clear_pending_handshake(self, cn, delay=8):
        """Limpia el flag de handshake pendiente después de un delay"""
        await asyncio.sleep(delay)
        if cn in self.pending_handshakes:
            self.pending_handshakes.remove(cn)
    
    async def _retry_pending_messages(self):
        """Reintenta enviar mensajes pendientes periódicamente SOLO si ya existe sesión guardada"""
        await asyncio.sleep(5)  # Esperar 5 segundos antes del primer intento
        
        while True:
            await asyncio.sleep(10)  # Reintentar cada 10 segundos
            
            for cn in list(self.contact_keys):
                info = self.db.get_contact_info(cn)
                if not info:
                    continue
                    
                ip = info.get("ip")
                port = info.get("port")
                
                # Solo intentar si tiene IP y puerto
                if not ip or not port:
                    continue
                
                # Verificar si tiene sesión guardada (requisito para auto-reconectar)
                session_key = self.db.get_session_key(cn)
                
                # SOLO intentar reconectar si YA hay sesión guardada
                if not session_key:
                    continue
                
                # Obtener mensajes pendientes
                pending = self.db.get_pending_messages(cn)
                
                if not pending:
                    continue
                
                # Verificar si la sesión está activa en memoria
                if self.protocol.tiene_sesion(ip, port):
                    # Intentar enviar mensajes con la sesión existente
                    success_count = 0
                    for msg in pending:
                        if self.protocol.enviar_mensaje(ip, port, msg['text'], msg['id']):
                            self.db.mark_message_status(cn, msg['id'], "sent")
                            success_count += 1
                        else:
                            # Si falla el envío, salir del bucle
                            break
                    
                    # Si se envió al menos uno, marcar como conectado
                    if success_count > 0:
                        # Verificar si ya estaba desconectado antes
                        was_disconnected = not info.get("is_connected")
                        
                        self.db.set_contact_connected(cn, True)
                        
                        # Solo mostrar mensaje si estaba desconectado
                        if was_disconnected:
                            ts = datetime.now().strftime("%H:%M")
                            self.db.add_message(cn, "Sys", f"Reconectado. {success_count} mensaje(s) enviado(s).", "system", ts)
                        
                        self.refresh_ui()
    
    async def _check_ack_timeouts(self):
        """Verifica periódicamente si hay mensajes sin ACK que indiquen desconexión"""
        while True:
            await asyncio.sleep(2)  # Verificar cada 2 segundos
            
            # Revisar todos los contactos conectados
            for cn in list(self.contact_keys):
                info = self.db.get_contact_info(cn)
                if info and info.get("is_connected"):
                    # Verificar si hay timeouts (mensajes sin ACK)
                    has_timeout = self.db.check_message_timeouts(cn, timeout_seconds=5)
                    
                    if has_timeout:
                        # El contacto no responde - marcar como desconectado
                        self.db.set_contact_connected(cn, False)
                        
                        # Cerrar sesión en el protocolo
                        if info.get("ip") and info.get("port"):
                            self.protocol.cerrar_sesion(info["ip"], info["port"])
                        
                        # Marcar mensajes 'sent' como 'pending' para mostrar reloj 🕒
                        msgs = self.db.get_history(cn)
                        for msg in msgs:
                            if msg.get("status") == "sent":
                                self.db.mark_message_status(cn, msg["id"], "pending")

                        # Agregar mensaje informativo
                        ts = datetime.now().strftime("%H:%M")
                        self.db.add_message(cn, "Sys", "Conexión perdida (sin respuesta). Mensajes en cola.", "error", ts)
                        
                        # Actualizar UI
                        self.refresh_ui()
    
    async def run(self):
        # Iniciar tareas de verificación de timeouts y reintentos
        self._timeout_check_task = asyncio.create_task(self._check_ack_timeouts())
        self._retry_task = asyncio.create_task(self._retry_pending_messages())
        
        try:
            await self.app.run_async()
        finally:
            # Cancelar tareas al salir
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