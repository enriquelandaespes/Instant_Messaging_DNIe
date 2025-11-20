# gui.py
import asyncio
from datetime import datetime
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.key_binding import KeyBindings

class ChatGUI:
    def __init__(self, protocol, my_nick, db):
        self.protocol = protocol
        self.my_nick = my_nick
        self.db = db
        
        # Diccionario de contactos. Clave: Common Name (CN) o Nick
        # Estructura: { "CN": { "ip": "...", "port": 123, "connected": Bool, "msgs": [] } }
        self.contacts = {}
        self.contact_keys = [] 
        self.selected_idx = 0
        self.current_cn = None 

        # Widgets
        self.w_contacts = TextArea(focusable=False, width=30)
        self.w_chat = TextArea(focusable=False, scrollbar=True, wrap_lines=True)
        self.w_input = TextArea(height=3, prompt="> ", multiline=False)

        # Cargar contactos previos de la BD (aunque estén offline)
        # Nota: Una implementación real leería la tabla 'contacts', aquí simplificamos
        # para que se llenen conforme llegan mensajes o discovery.

        self.layout = Layout(HSplit([
            VSplit([
                Frame(self.w_contacts, title="Contactos (Flechas + Enter)"), 
                Frame(self.w_chat, title="Chat Seguro")
            ]),
            Frame(self.w_input, title=f"Mensaje ({my_nick})")
        ]))

        kb = KeyBindings()
        @kb.add("c-c")
        def _(event): event.app.exit()
        @kb.add("up")
        def _(event): self.move_selection(-1)
        @kb.add("down")
        def _(event): self.move_selection(1)
        @kb.add("enter")
        def _(event): self.handle_enter()

        self.app = Application(layout=self.layout, key_bindings=kb, full_screen=True, mouse_support=True)

    def move_selection(self, delta):
        if not self.contact_keys: return
        self.selected_idx = (self.selected_idx + delta) % len(self.contact_keys)
        self.current_cn = self.contact_keys[self.selected_idx]
        self.refresh_ui()

    def add_or_update_peer(self, name, ip, port, cn=None):
        """Llamado por Discovery (solo name/ip) o por Protocol (cn real)."""
        # Si viene de Discovery, name suele ser "Nick_Port". Usamos Nick como ID temporal si no tenemos CN.
        display_name = name.split('_')[0]
        key = cn if cn else display_name # Preferimos CN como clave

        if key not in self.contacts:
            self.contacts[key] = {
                "display": display_name,
                "ip": ip, 
                "port": port, 
                "connected": False,
                "cn": cn # Guardamos el CN real si lo tenemos
            }
            self.contact_keys.append(key)
            # Cargar historial si existe
            msgs = self.db.get_history(key)
            self.contacts[key]["msgs"] = msgs
        else:
            # Actualizar IP/Port si el usuario se movió
            self.contacts[key]["ip"] = ip
            self.contacts[key]["port"] = port
            if cn: self.contacts[key]["cn"] = cn

        # Verificar pendientes y enviar si es posible
        if cn:
            self.check_pending_messages(key)

        if self.current_cn is None:
            self.current_cn = key
        
        self.refresh_ui()

    def on_protocol_msg(self, addr, text, nombre_cn):
        # Si llega mensaje, aseguramos que existe el contacto por su CN
        self.add_or_update_peer(nombre_cn, addr[0], addr[1], cn=nombre_cn)
        contact = self.contacts[nombre_cn]
        
        timestamp = datetime.now().strftime("%H:%M")

        if text == "HANDSHAKE_OK":
            contact["connected"] = True
            self.db.add_message(nombre_cn, "Sys", "--- CONEXIÓN ESTABLECIDA ---", "received", timestamp)
            # Ahora que estamos conectados, enviamos pendientes
            self.check_pending_messages(nombre_cn)
        else:
            self.db.add_message(nombre_cn, nombre_cn, text, "received", timestamp)
        
        # Recargar historial de la BD para mostrar
        contact["msgs"] = self.db.get_history(nombre_cn)
        self.refresh_ui()

    def check_pending_messages(self, cn):
        contact = self.contacts[cn]
        # Solo intentamos enviar si tenemos IP y creemos estar "conectados" 
        # (o podemos intentar forzar si tenemos IP)
        if not contact["ip"]: return

        pending = self.db.get_pending_messages(cn)
        if pending:
            sent_ids = []
            # Si no estamos conectados, forzamos handshake primero en handle_enter, 
            # pero aquí asumimos que si check_pending se llama, algo pasó (discovery o msg).
            # Intentamos enviar.
            for msg in pending:
                self.protocol.enviar_mensaje(contact["ip"], contact["port"], msg["text"])
                sent_ids.append(msg["id"])
            
            self.db.mark_messages_sent(sent_ids)
            # Actualizar vista con nuevos estados
            contact["msgs"] = self.db.get_history(cn)

    def handle_enter(self):
        if not self.current_cn: return
        text = self.w_input.text.strip()
        self.w_input.text = ""
        if not text: return

        c = self.contacts[self.current_cn]
        ip, port = c["ip"], c["port"]
        timestamp = datetime.now().strftime("%H:%M")

        # Lógica de envío
        if not ip:
            # OFFLINE TOTAL (No Discovery)
            self.db.add_message(self.current_cn, "Yo", text, "pending", timestamp)
            c["msgs"] = self.db.get_history(self.current_cn)
            # Aviso visual opcional
            # self.contacts[self.current_cn]["msgs"].append({"text": "(Guardado offline)", "sender": "Sys", "time": ""})
        elif not c["connected"]:
            # Hay IP pero falta Handshake -> Iniciamos y guardamos como pendiente
            self.protocol.enviar_handshake(ip, port)
            self.db.add_message(self.current_cn, "Yo", text, "pending", timestamp)
            c["msgs"] = self.db.get_history(self.current_cn)
        else:
            # Conectado -> Enviar
            self.protocol.enviar_mensaje(ip, port, text)
            self.db.add_message(self.current_cn, "Yo", text, "sent", timestamp)
            c["msgs"] = self.db.get_history(self.current_cn)

        self.refresh_ui()

    def refresh_ui(self):
        # 1. Lista Contactos
        lines = []
        for k in self.contact_keys:
            c = self.contacts[k]
            prefix = "➤ " if k == self.current_cn else "  "
            # Icono estado
            icon = "🟢" if c.get("connected") else "⚫" # Verde Online, Negro/Gris Offline
            if not c["ip"]: icon = "🔴" # Rojo totalmente perdido
            
            name = c.get("display", k)
            lines.append(f"{prefix}{icon} {name}")
        self.w_contacts.text = "\n".join(lines)

        # 2. Chat
        if self.current_cn:
            msgs = self.contacts[self.current_cn].get("msgs", [])
            chat_lines = []
            width_est = 50 # Estimación para padding
            
            for m in msgs:
                time = f"[{m['time']}]"
                sender = m['sender']
                txt = m['text']
                
                if sender == "Yo":
                    # Alineación derecha simulada
                    # Icono de estado: ⏳ (Pending), ✓ (Sent)
                    status_icon = "⏳" if m['status'] == 'pending' else "✓"
                    line = f"{status_icon} {txt} {time}".rjust(width_est)
                elif sender == "Sys":
                    line = f"--- {txt} ---".center(width_est)
                else:
                    # Izquierda
                    line = f"{time} {sender}: {txt}"
                
                chat_lines.append(line)
            
            self.w_chat.text = "\n".join(chat_lines)
            
            c = self.contacts[self.current_cn]
            status = "ONLINE" if c["connected"] else "OFFLINE (Se enviará al conectar)"
            self.w_chat.title = f"Chat con {c.get('display')} [{status}]"
        else:
            self.w_chat.text = "Esperando..."

        self.app.invalidate()

    async def run(self):
        await self.app.run_async()