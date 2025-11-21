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
        self._last_cursor_y = 0 

        # --- Widgets ---
        self.w_contacts = TextArea(focusable=False, width=35)
        
        self.chat_control = FormattedTextControl(
            text=self._get_chat_content,
            get_cursor_position=self._get_chat_cursor_position,
            focusable=False 
        )
        
        self.w_chat_window = Window(
            content=self.chat_control, 
            wrap_lines=True, 
            always_hide_cursor=False
        )
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # --- Layout ---
        self.layout = Layout(
            HSplit([
                VSplit([
                    Frame(self.w_contacts, title="Vecinos (DNIe)"), 
                    Frame(self.w_chat_window, title=self._get_chat_title)
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

        self.app = Application(layout=self.layout, key_bindings=kb, full_screen=True, mouse_support=True)
        self._load_initial_contacts()

    def _get_chat_cursor_position(self):
        return Point(x=0, y=self._last_cursor_y)

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
        if not self.current_cn: 
            self._last_cursor_y = 0
            return [("class:info", "Esperando contactos...")]
        
        msgs = list(self.db.get_history(self.current_cn))
        formatted_lines = []
        PAD_WIDTH = 80 
        line_count = 0

        for m in msgs:
            sender, text, time, status = m.get('sender'), m.get('text'), m.get('time'), m.get('status', '')
            formatted_lines.append(("", "\n"))
            line_count += 1
            
            if sender == self.my_nick:
                if status == 'delivered': tick = "✓✓"
                elif status == 'sent': tick = "✓"
                else: tick = "🕒"
                line_content = f"{text}   {time} {tick}"
                padding = " " * max(0, PAD_WIDTH - len(line_content))
                formatted_lines.append(("", padding))
                formatted_lines.append(("ansicyan bold", line_content))
                
            elif sender == "Sys":
                center_pad = " " * max(0, (PAD_WIDTH - len(text)) // 2)
                formatted_lines.append(("ansigray", f"{center_pad}--- {text} ---"))
                
            else:
                formatted_lines.append(("ansiyellow", f"[{time}] {sender}:\n")) 
                line_count += 1 
                formatted_lines.append(("ansiyellow", f" > {text}")) 
        
        self._last_cursor_y = line_count
        return formatted_lines

    def move_selection(self, delta):
        if not self.contact_keys: return
        idx = self.contact_keys.index(self.current_cn) if self.current_cn in self.contact_keys else 0
        new_idx = (idx + delta) % len(self.contact_keys)
        self.current_cn = self.contact_keys[new_idx]
        self.refresh_ui()

    # --- CORRECCIÓN: Renombrado a add_peer para coincidir con main.py ---
    def add_peer(self, name, ip, port):
        # Verificar si ya existe por IP para actualizar datos
        real_exists_cn = None
        for cn, data in self.db.get_all_contacts().items():
            if data.get("ip") == ip:
                real_exists_cn = cn
                break
        
        if real_exists_cn:
            # Si existe y el nombre nuevo es diferente (y no es el ID por defecto), actualizamos
            if real_exists_cn != name and "dni-im" not in name:
                 # Opcional: Renombrar contacto en DB si se desea, o solo actualizar IP
                 pass
            self.db.add_or_update_contact(real_exists_cn, ip=ip, port=port)
        else:
            # Nuevo contacto
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
        elif text.startswith("ACK|"):
            # ACK recibido, actualizar mensaje a "delivered" (doble tick)
            msg_id = text.split('|', 1)[1]
            self.db.mark_message_status(real_cn, msg_id, "delivered")
        else:
            self.db.set_contact_connected(real_cn, True)
            self.db.add_message(real_cn, real_cn, text, "received", ts)
            
        self.refresh_ui()

    def on_ack_received(self, cn, msg_id):
        self.db.mark_message_status(cn, msg_id, "delivered")
        self.app.invalidate()

    def check_pending_messages(self, cn, ip, port):
        """Envía todos los mensajes pendientes en cola cuando se reconecta"""
        pending = self.db.get_pending_messages(cn)
        if not pending: 
            return
        
        # Agregar mensaje de debug
        ts = datetime.now().strftime("%H:%M")
        self.db.add_message(cn, "Sys", f"Enviando {len(pending)} mensaje(s) pendiente(s)...", "system", ts)
        
        for i, msg in pending:
            if self.protocol.enviar_mensaje(ip, port, msg["text"], msg["id"]):
                self.db.mark_message_status(cn, msg["id"], "sent")
            else:
                # Si falla, mantener como pending
                self.db.add_message(cn, "Sys", f"Error al reenviar mensaje: {msg['text'][:30]}...", "error", ts)
        
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
                self.db.add_message(self.current_cn, "Sys", "Usuario Offline - Sin IP", "error", ts)
            self.w_input.text = ""
            self.refresh_ui()
            return

        # Si no está conectado, intentar conectar (con o sin mensaje)
        if not info.get("is_connected"):
            # Si hay texto, guardarlo en cola
            if text:
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", ts)
                self.w_input.text = ""
            
            # Marcar como desconectado y actualizar UI inmediatamente
            self.db.set_contact_connected(self.current_cn, False)
            # Cerrar sesión vieja en el protocolo si existe
            self.protocol.cerrar_sesion(ip, port)
            self.refresh_ui()  # Actualizar para mostrar 🔴 inmediatamente
            
            # Intentar reconectar si no está ya intentando
            if self.current_cn not in self.pending_handshakes:
                self.protocol.enviar_handshake(ip, port)
                self.pending_handshakes.add(self.current_cn)
                
                if text:
                    self.db.add_message(self.current_cn, "Sys", "Destinatario desconectado. Mensaje en cola.", "system", ts)
                else:
                    self.db.add_message(self.current_cn, "Sys", "Intentando conectar...", "system", ts)
                
                self.refresh_ui()
                
                # Timeout de reconexión
                await asyncio.sleep(8)
                if self.current_cn in self.pending_handshakes:
                    self.pending_handshakes.remove(self.current_cn)
                    if text:
                        self.db.add_message(self.current_cn, "Sys", "No se pudo conectar. Mensaje guardado en cola.", "error", ts)
                    else:
                        self.db.add_message(self.current_cn, "Sys", "No se pudo conectar.", "error", ts)
                    self.refresh_ui()
            else:
                if text:
                    self.db.add_message(self.current_cn, "Sys", "Ya intentando conectar. Mensaje en cola.", "system", ts)
                self.refresh_ui()
            return

        # Si está conectado y hay texto, enviar
        if text:
            # Verificar que realmente haya sesión en el protocolo
            if not self.protocol.tiene_sesion(ip, port):
                # No hay sesión activa, marcar como desconectado
                self.db.set_contact_connected(self.current_cn, False)
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", ts)
                self.db.add_message(self.current_cn, "Sys", "Sesión perdida. Reconectando...", "error", ts)
                self.w_input.text = ""
                self.refresh_ui()
                
                # Intentar reconectar
                if self.current_cn not in self.pending_handshakes:
                    self.protocol.enviar_handshake(ip, port)
                    self.pending_handshakes.add(self.current_cn)
                return
            
            msg_id = self.db.add_message(self.current_cn, self.my_nick, text, "sent", ts)
            self.w_input.text = ""
            
            if not self.protocol.enviar_mensaje(ip, port, text, msg_id):
                # Si falla el envío, marcar como desconectado y cambiar estado a pending
                self.db.mark_message_status(self.current_cn, msg_id, "pending")
                self.db.set_contact_connected(self.current_cn, False)
                self.protocol.cerrar_sesion(ip, port)  # Limpiar sesión
                self.db.add_message(self.current_cn, "Sys", "Fallo de envío. Mensaje en cola.", "error", ts)
                self.refresh_ui()  # Actualizar para mostrar 🔴
                
                # Intentar reconectar
                if self.current_cn not in self.pending_handshakes:
                    self.protocol.enviar_handshake(ip, port)
                    self.pending_handshakes.add(self.current_cn)
                
            self.refresh_ui()
        # Si no hay texto y ya está conectado, no hacer nada (ya conectado)

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