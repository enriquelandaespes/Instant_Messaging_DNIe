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
        
        # contacts_state ahora solo guarda info para la UI
        # Los datos persistentes (IP, Port) están en db.data["contacts"]
        self.contacts_state = {} 
        self.current_cn = None # CN del contacto seleccionado
        self.contact_keys = [] # Lista ordenada de CNs para la UI

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
        for cn in self.db.data["contacts"]:
            self._init_contact_state(cn) # Inicializa el estado para la UI
        
        # Ordenar y seleccionar el primero si existe
        self.contact_keys.sort()
        if self.contact_keys and self.current_cn is None:
            self.current_cn = self.contact_keys[0]
        self.refresh_ui()

    def _init_contact_state(self, cn):
        # Esta función inicializa/reinicia el estado de un contacto en la UI
        # La IP y el puerto se obtienen de la DB. El estado 'connected' es inicial falso.
        db_contact = self.db.get_contact_info(cn)
        self.contacts_state[cn] = {
            "connected": False, # Estado inicial no conectado
            "display_name": cn,
            "ip": db_contact.get("ip"),
            "port": db_contact.get("port")
        }
        if cn not in self.contact_keys:
            self.contact_keys.append(cn)
            self.contact_keys.sort() # Mantener ordenado

    def _get_chat_title(self):
        if not self.current_cn: return "Chat Seguro"
        
        state = self.contacts_state.get(self.current_cn, {})
        status_text = ""
        
        if state.get("connected"):
             status_text = "🟢 CONECTADO"
        elif state.get("ip"): # Tiene IP conocida pero no conectado (solo descubierto)
             status_text = "🟡 DISPONIBLE (Pulsa Enter)"
        else:
             status_text = "🔴 OFFLINE"
             
        return f"Chat con {self.current_cn} [{status_text}]" # Usar current_cn directamente aquí

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

    def add_or_update_peer(self, discovered_name_or_cn, ip, port, from_zeroconf=True):
        """
        Gestiona la adición/actualización de un peer.
        'discovered_name_or_cn' puede ser un 'nick_puerto' de Zeroconf o un CN directo.
        """
        cn_to_use = None
        
        if from_zeroconf:
            # Si viene de Zeroconf, el nombre es 'nick_puerto'
            # Buscamos si ya tenemos un contacto con esa IP/Puerto en la DB
            for existing_cn, data in self.db.data["contacts"].items():
                if data.get("ip") == ip and data.get("port") == port:
                    cn_to_use = existing_cn
                    break
            
            # Si no, asumimos que el 'nick_puerto' es un nuevo contacto temporal
            # o que el CN no se ha extraído todavía
            if not cn_to_use:
                cn_to_use = discovered_name_or_cn # Usamos el nombre de Zeroconf temporalmente
        else:
            # Si viene del protocolo (ya tenemos un CN del certificado)
            cn_to_use = discovered_name_or_cn
        
        if not cn_to_use: return # No se pudo determinar el CN

        # Añadir/actualizar en la DB
        actual_cn = self.db.add_or_update_contact(cn_to_use, ip, port)
        
        # Inicializar/actualizar el estado de la UI
        if actual_cn not in self.contacts_state:
            self._init_contact_state(actual_cn)
        else:
            self.contacts_state[actual_cn]["ip"] = ip
            self.contacts_state[actual_cn]["port"] = port
        
        # Reiniciar estado de conexión si es una nueva IP/puerto o de zeroconf
        if from_zeroconf:
            self.contacts_state[actual_cn]["connected"] = False

        self.refresh_ui()

    def on_protocol_msg(self, addr, text, nombre_cn):
        # addr es la tupla (IP, Puerto)
        # nombre_cn es el nombre extraído del certificado del handshake
        
        # Primero, actualizamos el contacto en la DB con el CN real y la IP/Puerto
        actual_cn = self.db.add_or_update_contact(nombre_cn, addr[0], addr[1])
        
        # Luego, actualizamos el estado de la GUI
        if actual_cn not in self.contacts_state:
            self._init_contact_state(actual_cn) # Lo añadimos si es nuevo para la GUI

        self.contacts_state[actual_cn]["ip"] = addr[0]
        self.contacts_state[actual_cn]["port"] = addr[1]

        timestamp = datetime.now().strftime("%H:%M")
        
        if text == "HANDSHAKE_OK":
            self.contacts_state[actual_cn]["connected"] = True # Marcar como conectado
            self.db.add_message(actual_cn, "Sys", "CONEXIÓN SEGURA ESTABLECIDA", "received", timestamp)
            self.check_pending(actual_cn, addr[0], addr[1])
        elif text == "HANDSHAKE_ERROR":
            self.contacts_state[actual_cn]["connected"] = False # Marcar como no conectado
            self.db.add_message(actual_cn, "Sys", "ERROR: No se pudo establecer conexión segura.", "received", timestamp)
        else:
            self.db.add_message(actual_cn, nombre_cn, text, "received", timestamp)
        
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
        
        current_contact_state = self.contacts_state[self.current_cn]
        
        ip = current_contact_state.get("ip")
        port = current_contact_state.get("port")
        
        # Si no hay IP/Puerto conocido, no podemos hacer nada (ni handshake ni mensaje)
        if not ip or not port:
            if text:
                self.db.add_message(self.current_cn, "Yo", text, "pending", datetime.now().strftime("%H:%M"))
                self.db.add_message(self.current_cn, "Sys", "Mensaje en cola. Vecino offline o no descubierto.", "Sys", datetime.now().strftime("%H:%M"))
            self.w_input.text = ""
            self.refresh_ui()
            return

        timestamp = datetime.now().strftime("%H:%M")

        if not current_contact_state["connected"]:
            # No estamos conectados -> intentar Handshake
            self.protocol.enviar_handshake(ip, port)
            self.db.add_message(self.current_cn, "Sys", "Enviando solicitud de conexión...", "pending", timestamp)
            if text: # Si hay texto, lo ponemos en cola como pendiente
                self.db.add_message(self.current_cn, "Yo", text, "pending", timestamp)
        else:
            # Ya estamos conectados -> enviar mensaje
            if text:
                self.protocol.enviar_mensaje(ip, port, text)
                self.db.add_message(self.current_cn, "Yo", text, "sent", timestamp)
            else:
                # Si estamos conectados y el input está vacío, simplemente ignorar Enter
                self.w_input.text = ""
                self.refresh_ui()
                return # Salir sin limpiar input de nuevo
        
        self.w_input.text = "" # Limpiar input después de procesar
        self.refresh_ui()

    def refresh_ui(self):
        lines = []
        for k in self.contact_keys:
            s = self.contacts_state[k]
            prefix = "➤ " if k == self.current_cn else "  "
            
            icon = "🔴" # Por defecto offline
            if s.get("connected"):
                icon = "🟢" 
            elif s.get("ip") and s.get("port"): # Tiene IP/Puerto pero no conectado (solo descubierto)
                icon = "🟡" 

            lines.append(f"{prefix}{icon} {s['display_name']}")
        
        self.w_contacts.text = "\n".join(lines)
        self.app.invalidate()

    async def run(self):
        await self.app.run_async()