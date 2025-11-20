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
        
        self.contact_keys = [] 
        self.current_cn = None 

        self.w_contacts = TextArea(focusable=False, width=35)
        self.chat_control = FormattedTextControl(
            text=self._get_chat_content,
            get_cursor_position=self._get_chat_cursor_position,
            focusable=False 
        )
        self.w_chat_window = Window(content=self.chat_control, wrap_lines=True, always_hide_cursor=False)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

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

        self.app = Application(
            layout=self.layout, 
            key_bindings=kb, 
            full_screen=True, 
            mouse_support=True
        )
        
        self._load_initial_contacts()

    def _get_chat_cursor_position(self):
        lines = self._get_chat_content()
        row_count = 0
        for item in lines:
            if item[1] == "\n": 
                row_count += 1
            else:
                row_count += item[1].count('\n') 
        return Point(x=0, y=row_count)

    def _load_initial_contacts(self):
        for cn in self.db.get_all_contacts().keys():
            if cn not in self.contact_keys:
                self.contact_keys.append(cn)
        
        self.contact_keys.sort()
        if self.contact_keys and self.current_cn is None:
            self.current_cn = self.contact_keys[0]
        self.refresh_ui()

    def _get_chat_title(self):
        if not self.current_cn: return "Chat Seguro"
        
        contact_info = self.db.get_contact_info(self.current_cn)
        
        status_text = ""
        if contact_info and contact_info.get("is_connected"):
             status_text = "🟢 CONECTADO"
        elif contact_info and contact_info.get("ip") and contact_info.get("port"):
             status_text = "🟡 DISPONIBLE" 
        else:
             status_text = "🔴 OFFLINE"
             
        return f"Chat con {self.current_cn} [{status_text}]"

    def _get_chat_content(self):
        if not self.current_cn: return [("class:info", "Esperando contactos...")]
        
        msgs = self.db.get_history(self.current_cn)
        formatted_lines = []
        PAD_WIDTH = 80 
        
        for m in msgs:
            sender = m.get('sender', 'Desconocido')
            text = m.get('text', '')
            time = m.get('time', '??:??')
            status = m.get('status', '')
            
            if sender == self.my_nick: 
                ticks = ""
                if status == 'pending': ticks = "✓" 
                elif status == 'sent': ticks = "✓" 
                elif status == 'received': ticks = "✓✓" 
                
                line_content = f"{text}   {time} {ticks}"
                padding = " " * max(0, PAD_WIDTH - len(line_content))
                formatted_lines.append(("", "\n"))
                formatted_lines.append(("", padding))
                formatted_lines.append(("ansicyan bold", f"{line_content}"))
            elif status == "system": # Mensajes del sistema
                formatted_lines.append(("", "\n"))
                formatted_lines.append(("ansigray", f"--- {text} ---".center(PAD_WIDTH)))
            elif status == "error": # Mensajes de error del sistema
                formatted_lines.append(("", "\n"))
                formatted_lines.append(("ansired", f"--- {text} ---".center(PAD_WIDTH)))
            else: # Mensajes de otros
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

    def add_or_update_peer(self, discovered_name, ip, port):
        cn_to_manage = discovered_name 

        # Si ya existe un CN con esta IP/Puerto en la DB, lo usamos
        found_cn_in_db = None
        for cn_key, data in self.db.get_all_contacts().items():
            if data.get("ip") == ip and data.get("port") == port:
                found_cn_in_db = cn_key
                break
        
        if found_cn_in_db:
            cn_to_manage = found_cn_in_db # Usar el CN real si ya lo conocíamos
        
        # Actualiza la DB. No cambia is_connected.
        self.db.add_or_update_contact(cn_to_manage, ip=ip, port=port, update_seen=True)
        
        if cn_to_manage not in self.contact_keys:
            self.contact_keys.append(cn_to_manage)
            self.contact_keys.sort()
        
        if self.current_cn is None:
            self.current_cn = cn_to_manage

        self.refresh_ui()

    async def on_protocol_msg(self, addr, text, real_cn_from_cert):
        # Asegurar que el contacto existe en la DB con el CN real
        self.db.add_or_update_contact(real_cn_from_cert, ip=addr[0], port=addr[1], update_seen=True)
        
        if real_cn_from_cert not in self.contact_keys:
            self.contact_keys.append(real_cn_from_cert)
            self.contact_keys.sort()
            
        timestamp = datetime.now().strftime("%H:%M")

        if text == "HANDSHAKE_OK":
            self.db.set_contact_connected(real_cn_from_cert, True)
            # Solo añadir el mensaje de "CONEXIÓN ESTABLECIDA" una vez
            if not any(m.get('text') == "CONEXIÓN SEGURA ESTABLECIDA" and m.get('sender') == "Sys" for m in self.db.get_history(real_cn_from_cert)):
                 self.db.add_message(real_cn_from_cert, "Sys", "CONEXIÓN SEGURA ESTABLECIDA", "system", timestamp)
            await self.check_pending_messages(real_cn_from_cert, addr[0], addr[1]) # Enviar pendientes

        elif text == "HANDSHAKE_START":
            # Si el handshake fue iniciado por nosotros o por el otro, se registra en el chat
            if not any(m.get('text') == "Iniciando conexión segura..." and m.get('sender') == "Sys" for m in self.db.get_history(real_cn_from_cert)):
                self.db.add_message(real_cn_from_cert, "Sys", "Iniciando conexión segura...", "system", timestamp)

        elif text == "HANDSHAKE_ERROR":
            self.db.set_contact_connected(real_cn_from_cert, False)
            self.db.add_message(real_cn_from_cert, "Sys", "ERROR: Falló la conexión segura.", "error", timestamp)
            
        elif text == "ERROR_CIFRADO":
             self.db.set_contact_connected(real_cn_from_cert, False)
             self.db.add_message(real_cn_from_cert, "Sys", "ERROR: Mensaje corrupto o clave inválida.", "error", timestamp)

        else: # Mensaje normal recibido
            self.db.add_message(real_cn_from_cert, real_cn_from_cert, text, "received", timestamp)
            self.db.set_contact_connected(real_cn_from_cert, True) # Confirmar que la conexión está activa

        self.refresh_ui()

    async def check_pending_messages(self, cn, ip, port):
        pending = self.db.get_pending_messages(cn)
        if not pending: return
        
        sent_indices = []
        for i, msg in pending:
            await asyncio.sleep(0.05) # Pequeño retraso para evitar sobrecargar
            self.protocol.enviar_mensaje(ip, port, msg["text"])
            sent_indices.append(i)
        
        if sent_indices:
            self.db.mark_message_status(cn, sent_indices, "sent")
            self.refresh_ui()

    async def handle_enter(self):
        if not self.current_cn: return
        text = self.w_input.text.strip()
        
        contact_info = self.db.get_contact_info(self.current_cn)
        if not contact_info: 
            self.w_input.text = ""
            self.refresh_ui()
            return

        ip = contact_info.get("ip")
        port = contact_info.get("port") # <--- CORREGIDO: Asegurarse de usar .get()
        is_connected = contact_info.get("is_connected")
        
        timestamp = datetime.now().strftime("%H:%M")

        # CASO 1: No tenemos IP/Puerto (Offline total)
        if not ip or not port:
            if text:
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", timestamp)
                self.db.add_message(self.current_cn, "Sys", "Mensaje en cola. Esperando descubrir usuario...", "system", timestamp)
            self.w_input.text = ""
            self.refresh_ui()
            return

        # CASO 2: Tenemos IP pero NO conexión segura (ni handshake en curso) -> Iniciar Handshake
        # Solo iniciar si no está conectado y si el protocolo no tiene un handshake ya iniciado/en respuesta
        if not is_connected and self.protocol.handshake_status.get(self.current_cn) not in ["INITIATED", "RESPONSED"]:
            self.protocol.enviar_handshake(ip, port)
            
            if text:
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", timestamp)
            
            self.w_input.text = ""
            self.refresh_ui()
            return
        
        # CASO 3: Hay handshake en progreso o ya estamos conectados -> Enviar mensaje o poner en cola
        if text:
            if is_connected: # Si ya hay conexión segura, enviar directamente
                self.protocol.enviar_mensaje(ip, port, text)
                self.db.add_message(self.current_cn, self.my_nick, text, "sent", timestamp)
            else: # Si hay handshake en curso (o falló y estamos esperando), poner en cola
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", timestamp)
                self.db.add_message(self.current_cn, "Sys", "Mensaje en cola. Conexión en progreso...", "system", timestamp)
            
            self.w_input.text = ""
            self.refresh_ui()

    def refresh_ui(self):
        lines = []
        all_contact_cns = sorted(list(self.contact_keys)) 

        for cn_key in all_contact_cns:
            contact_info = self.db.get_contact_info(cn_key)
            if not contact_info: continue

            icon = "🔴" 
            if contact_info.get("is_connected"):
                icon = "🟢" 
            elif contact_info.get("ip") and contact_info.get("port"): 
                icon = "🟡" 

            prefix = "➤ " if cn_key == self.current_cn else "  "
            lines.append(f"{prefix}{icon} {cn_key}")
        
        self.w_contacts.text = "\n".join(lines)
        self.app.invalidate()

    async def run(self):
        await self.app.run_async()