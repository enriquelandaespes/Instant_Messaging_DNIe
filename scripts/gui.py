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
        self.protocol = protocol # Será asignado después en main.py
        self.my_nick = my_nick
        self.db = db
        
        # contacts_state para la UI: {CN: {"display_name": "...", "connected": bool}}
        self.contacts_state = {} 
        self.contact_keys = [] # Lista ordenada de CNs para la UI
        self.current_cn = None # CN del contacto seleccionado

        # --- Widgets ---
        self.w_contacts = TextArea(focusable=False, width=35)
        self.chat_control = FormattedTextControl(
            text=self._get_chat_content,
            get_cursor_position=self._get_chat_cursor_position,
            focusable=False 
        )
        self.w_chat_window = Window(content=self.chat_control, wrap_lines=True, always_hide_cursor=False)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # --- ESTRUCTURA Y FOCO INICIAL ---
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
        def _(event): self.handle_enter()

        # --- Aplicación ---
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
            if item[1] == "\n": row_count += 1
        return Point(x=0, y=row_count)

    def _load_initial_contacts(self):
        # Cargar todos los contactos de la DB al iniciar
        for cn, contact_data in self.db.data["contacts"].items():
            self._init_contact_ui_state(cn) # Inicializa el estado para la UI
        
        self.contact_keys.sort()
        if self.contact_keys and self.current_cn is None:
            self.current_cn = self.contact_keys[0]
        self.refresh_ui()

    def _init_contact_ui_state(self, cn):
        # Inicializa o actualiza el estado de la UI para un contacto dado su CN
        # connected se asume False al inicio
        if cn not in self.contacts_state:
            self.contacts_state[cn] = {"display_name": cn, "connected": False}
            if cn not in self.contact_keys:
                self.contact_keys.append(cn)
                self.contact_keys.sort()
        # Si ya existe, solo resetear el estado de conexión al iniciar
        self.contacts_state[cn]["connected"] = False

    def _get_chat_title(self):
        if not self.current_cn: return "Chat Seguro"
        
        ui_state = self.contacts_state.get(self.current_cn, {})
        db_info = self.db.get_contact_info(self.current_cn)
        
        status_text = ""
        
        if ui_state.get("connected"):
             status_text = "🟢 CONECTADO"
        elif db_info and db_info.get("ip") and db_info.get("port"):
             status_text = "🟡 DISPONIBLE (Pulsa Enter)"
        else:
             status_text = "🔴 OFFLINE"
             
        return f"Chat con {ui_state.get('display_name', self.current_cn)} [{status_text}]"

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
            
            if sender == self.my_nick: # Mensajes propios
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
        """
        Llamado por DiscoveryService.
        'discovered_name' es del formato 'nick_puerto' de Zeroconf.
        """
        # Intentamos encontrar el CN si ya lo tenemos por IP/Puerto, si no, usamos 'discovered_name' temporalmente
        cn_from_db = None
        for cn_key, data in self.db.data["contacts"].items():
            if data.get("ip") == ip and data.get("port") == port:
                cn_from_db = cn_key
                break
        
        # Si no lo encontramos por IP/Puerto, o si es un nuevo descubrimiento,
        # asumimos que el 'discovered_name' (e.g., "SanzManovel_6666") es el identificador temporal
        # hasta que se establezca el handshake y conozcamos el CN real del certificado.
        # En la DB, lo guardaremos como 'discovered_name' si no tenemos un CN real.
        managed_cn = cn_from_db if cn_from_db else discovered_name

        # 1. Actualizar la base de datos con la información del contacto
        # Esto creará el contacto si es nuevo o actualizará su IP/Puerto
        self.db.add_or_update_contact(managed_cn, ip, port)
        
        # 2. Actualizar el estado de la UI
        if managed_cn not in self.contacts_state:
            self._init_contact_ui_state(managed_cn)
        
        self.contacts_state[managed_cn]["display_name"] = managed_cn # Display temporal de Zeroconf
        self.contacts_state[managed_cn]["connected"] = False # Siempre falso en el descubrimiento
        
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, real_cn_from_cert):
        # addr es (IP, Puerto), real_cn_from_cert es el Common Name del DNIe
        
        # 1. Asegurarnos de que el contacto existe en la DB con el CN real
        # Si ya teníamos un contacto temporal (nick_puerto) para esa IP/Puerto, lo actualizamos al CN real.
        managed_cn = self.db.add_or_update_contact(real_cn_from_cert, addr[0], addr[1])
        
        # Si el CN real es diferente del temporal que habíamos usado para esa IP/Puerto,
        # necesitamos "migrar" los mensajes antiguos o al menos actualizar el nombre en la UI.
        # Por simplicidad, asumimos que 'real_cn_from_cert' es el definitivo.
        
        # 2. Actualizar el estado de la UI
        if managed_cn not in self.contacts_state:
            self._init_contact_ui_state(managed_cn)

        self.contacts_state[managed_cn]["display_name"] = real_cn_from_cert # Mostrar el nombre real
        
        timestamp = datetime.now().strftime("%H:%M")
        
        if text == "HANDSHAKE_OK":
            self.contacts_state[managed_cn]["connected"] = True 
            self.db.add_message(managed_cn, "Sys", "CONEXIÓN SEGURA ESTABLECIDA", "received", timestamp)
            self.check_pending(managed_cn, addr[0], addr[1])
        elif text == "HANDSHAKE_ERROR":
            self.contacts_state[managed_cn]["connected"] = False 
            self.db.add_message(managed_cn, "Sys", "ERROR: No se pudo establecer conexión segura.", "received", timestamp)
        else:
            self.db.add_message(managed_cn, managed_cn, text, "received", timestamp)
        
        self.refresh_ui()

    def check_pending(self, cn, ip, port):
        pending = self.db.get_pending_messages(cn)
        if not pending or not ip or not port: return
        
        sent_indices = []
        for i, msg in pending:
            # Asegurarse de que solo enviamos si estamos conectados
            if self.contacts_state.get(cn, {}).get("connected"):
                self.protocol.enviar_mensaje(ip, port, msg["text"])
                sent_indices.append(i)
        
        if sent_indices:
            self.db.mark_as_sent(cn, sent_indices)
            self.refresh_ui()

    def handle_enter(self):
        if not self.current_cn: return
        text = self.w_input.text.strip()
        
        db_info = self.db.get_contact_info(self.current_cn)
        ui_state = self.contacts_state.get(self.current_cn, {})
        
        ip = db_info.get("ip") if db_info else None
        port = db_info.get("port") if db_info else None
        
        # Si no hay IP/Puerto conocido, no podemos hacer nada
        if not ip or not port:
            if text:
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", datetime.now().strftime("%H:%M"))
                self.db.add_message(self.current_cn, "Sys", "Mensaje en cola. Vecino offline o no descubierto.", "Sys", datetime.now().strftime("%H:%M"))
            self.w_input.text = ""
            self.refresh_ui()
            return

        timestamp = datetime.now().strftime("%H:%M")

        if not ui_state.get("connected"):
            # No estamos conectados -> intentar Handshake
            self.protocol.enviar_handshake(ip, port)
            self.db.add_message(self.current_cn, "Sys", "Enviando solicitud de conexión...", "pending", timestamp)
            if text: # Si hay texto, lo ponemos en cola como pendiente
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", timestamp)
        else:
            # Ya estamos conectados -> enviar mensaje
            if text:
                self.protocol.enviar_mensaje(ip, port, text)
                self.db.add_message(self.current_cn, self.my_nick, text, "sent", timestamp)
            else:
                # Si estamos conectados y el input está vacío, ignorar Enter
                self.w_input.text = ""
                self.refresh_ui()
                return 
        
        self.w_input.text = "" 
        self.refresh_ui()

    def refresh_ui(self):
        lines = []
        # Contactos cargados desde la DB + los nuevos descubiertos
        all_contact_cns = sorted(list(self.contacts_state.keys())) 

        for cn_key in all_contact_cns:
            ui_state = self.contacts_state[cn_key]
            db_info = self.db.get_contact_info(cn_key) # Obtener info de la DB para IP/Port
            
            prefix = "➤ " if cn_key == self.current_cn else "  "
            
            icon = "🔴" # Por defecto offline
            if ui_state.get("connected"):
                icon = "🟢" 
            elif db_info and db_info.get("ip") and db_info.get("port"): 
                icon = "🟡" 

            lines.append(f"{prefix}{icon} {ui_state['display_name']}")
        
        self.w_contacts.text = "\n".join(lines)
        self.app.invalidate()

    async def run(self):
        await self.app.run_async()