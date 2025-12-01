# Importación de módulos necesarios para la interfaz gráfica de texto (TUI)
import asyncio  
import json  
import os  
import unicodedata  # Para calcular el ancho visual de caracteres Unicode (emojis ocupan 2 espacios)
from datetime import datetime, timedelta  # Para gestionar marcas de tiempo (timestamps) de los mensajes

# Importaciones de prompt_toolkit
from prompt_toolkit.application import Application  # Aplicación principal que gestiona toda la TUI
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window  # Componentes para organizar la interfaz (vertical/horizontal)
from prompt_toolkit.widgets import TextArea, Frame  # Widgets: áreas de texto y marcos decorativos
from prompt_toolkit.layout.controls import FormattedTextControl  # Control para mostrar texto con formato (colores, estilos)
from prompt_toolkit.key_binding import KeyBindings  # Sistema para capturar atajos de teclado (Ctrl+C, Enter, etc.)
from prompt_toolkit.data_structures import Point  # Estructura para indicar posición del cursor (x, y)
from prompt_toolkit.styles import Style  # Sistema de estilos CSS-like para colorear la interfaz

class ChatTUI: # Clase principal de la interfaz de usuario (TUI = Text User Interface)
    def __init__(self, protocol, my_nick, db, my_ip="0.0.0.0", my_port=0):  # Constructor: inicializa la TUI
        # Parámetros de inicialización
        self.protocol = protocol  # Referencia al protocolo de red
        self.my_nick = my_nick  # Nombre de usuario (extraído del DNIe)
        self.db = db  # Base de datos para almacenar contactos e historial de mensajes
        self.my_ip = my_ip  # Nuestra dirección IP local (obtenida al iniciar el servidor UDP)
        self.my_port = my_port  # Puerto UDP en el que escuchamos conexiones
        self.contact_keys = []  # Lista de identificadores de contactos (ip:puerto o nombre)
        self.current_cn = None  # Contacto actualmente seleccionado en la interfaz
        self.pending_handshakes = set()  # Conjunto de contactos con handshake
        self._timeout_check_task = None  # Tarea que verifica timeouts de ACKs no recibidos
        self._reconnect_timeout_task = None  # Tarea que intenta reconectar sesiones perdidas
        self._window_monitor_task = None  # Tarea que detecta cambios de tamaño de ventana
        self._ui_refresh_task = None  # Tarea que fuerza redibujo constante de la UI
        self.sending_pending = set()  # Contactos que están actualmente enviando mensajes pendientes
        self.pending_sent = {}  # Diccionario (ip, port) -> bool indicando si ya enviamos nuestros pendientes
        self._last_line_count = 0  # Número de líneas del chat en la última actualización
        self.scroll_offset = 0  # Desplazamiento vertical del scroll 
        self._last_window_width = 0  # Ancho de la ventana de chat en la última actualización
        
        # Sistema de ASCII Art: permite enviar dibujos predefinidos en los mensajes
        self.ascii_art = {}  # Diccionario que almacenará la relacion clave -> dibujo ASCII
        try:
            ascii_path = os.path.join(os.path.dirname(__file__), 'ascii.json')  # Ruta al archivo JSON con arte ASCII
            with open(ascii_path, 'r', encoding='utf-8') as f:  
                data = json.load(f)  
                self.ascii_art = data.get('ascii', {})  # Extraemos la clave 'ascii' del JSON
        except Exception as e:
            print(f"Error cargando ascii.json: {e}")

        self.w_contacts = TextArea(focusable=False, width=35)  # TextArea de solo lectura, 35 caracteres de ancho
        
        self.chat_control = FormattedTextControl( 
            text=self.get_chat_content,  # Función que genera el texto formateado del chat
            get_cursor_position=self.get_safe_cursor_position,  # Función que calcula la posición del scroll
            focusable=False, 
            show_cursor=False 
        ) # Muestra el contenido del chat con formato
        
        self.w_chat_window = Window( 
            content=self.chat_control,
            wrap_lines=True,  # Habilita salto de línea automático si el mensaje es muy largo
            always_hide_cursor=True, 
            style="class:chat-bg",  # Aplicamos estilo CSS-like
            allow_scroll_beyond_bottom=False,  # No permitir scroll más allá del último mensaje
            dont_extend_height=False  # Permitir que la ventana ocupe todo el espacio disponible
        ) # Ventana que contiene el chat formateado
        
        self.w_ascii = TextArea(height=2, prompt="> ", multiline=True, width=35)
        self.w_input = TextArea(height=2, prompt="> ", multiline=True)
        self.w_suggestions = TextArea(focusable=False, height=1, style="fg:ansigray")
        
        # Callback que se ejecuta cada vez que el usuario escribe en el campo ASCII
        def on_ascii_text_changed(_):
            self.update_ascii_suggestions()  # Actualizar las sugerencias de ASCII Art en tiempo real
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
        ) # Layout completo de la TUI

        style = Style.from_dict({
            'chat-bg': '',  
            'msg-sent': "#C3C3C3",  
            'msg-recv': '#ff8800',  
            'msg-sys': '#888888 italic',  
            'time': '#5599ff bold',  
            'tick-sent': '#aaaaaa',  
            'tick-read': '#00ff00 bold',
        }) # Sistema de estilos CSS-like para colorear la interfaz

        kb = KeyBindings()  # Atajos de teclado
        @kb.add("c-c")  # Ctrl+C: salida limpia de la aplicación
        def _(e): e.app.exit()  
        @kb.add("up")  # Flecha arriba: navegar hacia arriba en la lista de contactos
        def _(e): self.move_selection(-1)  
        @kb.add("down")  # Flecha abajo: navegar hacia abajo en la lista de contactos
        def _(e): self.move_selection(1)  
        @kb.add("enter")  # Enter: acción principal (enviar mensaje o iniciar handshake)
        def _(e): asyncio.create_task(self.handle_enter())  
        @kb.add("c-d")  # Ctrl+D: desconectar manualmente del contacto actual
        def _(e): self.force_disconnect() 
        @kb.add("tab")  # Tab: alternar entre campo de mensajes y campo ASCII
        def _(e):
            if e.app.layout.has_focus(self.w_input):  
                e.app.layout.focus(self.w_ascii)  
            else:  
                e.app.layout.focus(self.w_input)  
        @kb.add("s-up")  # Shift+Arriba: scroll manual hacia arriba en el historial del chat
        def _(e):
            self.scroll_offset += 5  
            if self.scroll_offset > max(0, self._last_line_count - 1): 
                self.scroll_offset = max(0, self._last_line_count - 1)  
            e.app.invalidate()  
        @kb.add("s-down")  # Shift+Abajo: scroll manual hacia abajo en el historial del chat
        def _(e):
            self.scroll_offset -= 5  
            if self.scroll_offset < 0:  
                self.scroll_offset = 0  
            e.app.invalidate()  

        self.app = Application(
            layout=self.layout, 
            key_bindings=kb, 
            full_screen=True,
            mouse_support=True, 
            style=style
        ) # Aplicación principal de la TUI
        
        self.load_initial_contacts()  # Recupera todos los contactos guardados en el historial

    def get_safe_cursor_position(self):  # Calcula la posición del cursor para el sistema de scroll
        if self._last_line_count <= 1:  # Si el chat tiene 0 o 1 línea, no hay nada que scrollear
            return Point(0, 0)  # Cursor en el origen (esquina superior izquierda)
        
        max_offset = max(0, self._last_line_count - 1) # Calculamos el máximo offset permitido
        actual_offset = min(self.scroll_offset, max_offset) # Aplicamos el offset actual pero limitado al máximo permitido
        target_line = max(0, self._last_line_count - 1 - actual_offset)
        
        return Point(x=0, y=target_line)  # Devolvemos posición (columna 0, fila calculada)

    def load_initial_contacts(self):  # Carga los contactos desde la base de datos al iniciar la aplicación
        for cn in self.db.get_all_contacts().keys():  # cn = contact name
            if cn not in self.contact_keys:  # Evitar duplicados
                self.contact_keys.append(cn)  # Añadimos el contacto a la lista interna
        
        self.contact_keys.sort()  # Ordenamos alfabéticamente para visualización consistente

        if self.current_cn is None:
            self.current_cn = "__AYUDA__"  # Pestaña Inicial
        
        self.refresh_ui()  # Actualizamos la interfaz

    def get_chat_title(self):  # Genera el título dinámico del panel de chat
        if not self.current_cn:  # Estado inicial
            return "Chat Seguro"

        if self.current_cn == "__AYUDA__":
            return "❓ Ayuda - Atajos de Teclado"  # Pestaña con información de uso
        elif self.current_cn == "__MI_CUENTA__":
            return "👤 Mi Cuenta"  # Pestaña con info del propio Usuario
        
        info = self.db.get_contact_info(self.current_cn) # Obtenemos información del contacto desde la base de datos
        
        status = "🔴 DESCONECTADO"  # Por defecto asumimos desconectado
        if info:
            if info.get("is_connected"):  # Si hay conexión activa 
                status = "🟢 CONECTADO"  
            elif info.get("session_key"):  # Si existe clave de sesión guardada pero no está conectado
                status = "🔴 DESCONECTADO"  
            else:  # No hay sesión guardada
                status = "🟡 DISPONIBLE" 
        
        
        if self.current_cn in self.pending_handshakes: # Si hay un handshake en progreso, sobreescribir el estado
            status = "⏳ CONECTANDO..."
        
        full_name = info.get("name", self.current_cn) if info else self.current_cn  # Nombre completo desde BD
        name_parts = full_name.split()  # Dividimos por espacios
        
        if len(name_parts) >= 2:  # Si hay al menos 2 palabras (nombre y apellido)
            if "," in full_name:  # Formato "APELLIDOS, NOMBRE"
                parts = full_name.split(",")  # Dividimos por coma
                apellidos = parts[0].strip().split() 
                nombre = parts[1].strip().split()[0] if len(parts) > 1 else ""  
                display_name = f"{nombre} {apellidos[0]}" 
            else:  
                display_name = f"{name_parts[0]} {name_parts[1]}"  
        else:  
            display_name = full_name
        
        return f"Chat con {display_name} [{status}]"  # Título final del panel

    def get_chat_content(self):  # Genera el contenido formateado del chat (mensajes, timestamps, etc.)
        if not self.current_cn:  # Si no hay contacto seleccionado
            self._last_line_count = 1  # Actualizamos contador de líneas para el scroll
            return [("class:msg-sys", "Esperando contactos...")]  # Mensaje de espera
        
        if self.current_cn == "__AYUDA__":
            return self.get_help_content()  # Devuelve el texto de ayuda con atajos de teclado
        elif self.current_cn == "__MI_CUENTA__":
            return self.get_my_account_content()  # Devuelve información del usuario local
        
        msgs = list(self.db.get_history(self.current_cn))  # Recupera todos los mensajes con este contacto
        formatted_lines = []  # Lista de tuplas (estilo, texto) para prompt_toolkit
        
        formatted_lines.append(("", "\n")) # Añadimos un margen superior para que el chat no esté pegado al borde
        
        try: # Calculamos el ancho de la ventana de chat para alinear mensajes a la derecha
            PAD_WIDTH = self.w_chat_window.render_info.window_width if self.w_chat_window.render_info else 80
        except:  # Si render_info no está disponible, usar 80 caracteres por defecto
            PAD_WIDTH = 80
        
        PAD_WIDTH = max(40, PAD_WIDTH - 4)  # Reducimos 4 caracteres para dejar márgenes laterales (2 a cada lado)
        current_lines = 1  # Contador de líneas renderizadas (para el sistema de scroll)
        
        for m in msgs:  # Iteramos por cada mensaje en el historial
            sender = m.get('sender')  # Quién envió el mensaje (nick, "Sys", etc.)
            text = m.get('text')  # Contenido del mensaje
            timestamp_str = m.get('timestamp')  # Marca de tiempo (formato ISO o "HH:MM")
            status = m.get('status', '')  # Estado: 'sent', 'delivered', 'received', 'pending', etc.
            
            if timestamp_str: # Si hay marca de tiempo, intentamos parsearla
                try:
                    dt = datetime.fromisoformat(timestamp_str)  # Parseamos ISO format
                    time = dt.strftime("%H:%M")  # Extraemos solo hora:minuto
                    full_date = dt.strftime("%Y-%m-%d %H:%M")  # Fecha completa para cálculos
                except:
                    # Si falla el parseo, usar el string tal cual
                    time = timestamp_str if len(timestamp_str) <= 5 else "??:??"
                    full_date = None
            else:
                time = "??:??"  # Timestamp desconocido
                full_date = None
            
            formatted_time = self.format_timestamp(time, full_date) # Formateamos el timestamp para mostrar
            
            formatted_lines.append(("", "\n")) # Añadimos separación entre mensajes
            current_lines += 1
            
            MARGIN = "  "  # Margen izquierdo constante de 2 espacios

            if sender == "Sys":
                center_pad = " " * max(0, (PAD_WIDTH - self.visual_len(text)) // 2)# Calculamos padding para centrar el mensaje
                formatted_lines.append(("class:msg-sys", f"{MARGIN}{center_pad}--- {text} ---"))

            elif status == 'received' or sender != self.my_nick:
                formatted_lines.append(("class:msg-recv", f"{MARGIN}[{formatted_time}] {sender}:\n"))# Primera línea: timestamp y nombre del remitente
                current_lines += 1
                for line in text.split('\n'):  # Líneas del mensaje con prefijo " > "
                    formatted_lines.append(("class:msg-recv", f"{MARGIN} > {line}\n"))
                    current_lines += 1

            else:
                if status == 'delivered':  # ACK recibido del destinatario
                    tick = "✅"  
                elif status == 'sent':  # Enviado pero sin ACK aún
                    tick = "🕒"  
                else:  # Cualquier otro estado (pending, error, etc.)
                    tick = "🕒"
                
                text_lines = text.split('\n')  # Dividimos el mensaje en líneas (soporte multilínea)
                
                
                time_info = f"{formatted_time} {tick}"  # Calculamos el ancho visual del timestamp con el tick
                time_width = self.visual_len(time_info)  # Ancho en caracteres (emojis cuentan como 2)
                
                if len(text_lines) > 1:
                    for i, line in enumerate(text_lines):  # Iteramos línea por línea
                        line_width = self.visual_len(line)  # Ancho de esta línea específica

                        if i == len(text_lines) - 1:# ÚLTIMA LÍNEA del mensaje: intentamos poner timestamp aquí
                            if line_width + time_width + 3 <= PAD_WIDTH:  # +3 para espacios separadores
                                padding = " " * max(0, PAD_WIDTH - line_width - time_width - 3)# SÍ CABE: alineamos todo a la derecha en una sola línea
                                formatted_lines.append(("", MARGIN + padding))  # Espacios a la izquierda
                                formatted_lines.append(("class:msg-sent", line))  # Texto del mensaje
                                formatted_lines.append(("class:time", f"   {formatted_time} "))  # Timestamp
                                tick_style = "class:tick-read" if status == 'delivered' else "class:tick-sent"
                                formatted_lines.append((tick_style, f"{tick}\n"))  # Tick con salto de línea
                                current_lines += 1
                            else: # NO CABE: línea de texto aparte + timestamp en línea separada
                                padding = " " * max(0, PAD_WIDTH - line_width)
                                formatted_lines.append(("", MARGIN + padding + line + "\n")) # Línea de texto
                                current_lines += 1
                                time_padding = " " * max(0, PAD_WIDTH - time_width) # Nueva línea solo para timestamp (alineado a la derecha)
                                formatted_lines.append(("", MARGIN + time_padding))
                                formatted_lines.append(("class:time", f"{formatted_time} "))
                                tick_style = "class:tick-read" if status == 'delivered' else "class:tick-sent"
                                formatted_lines.append((tick_style, f"{tick}\n"))
                                current_lines += 1
                        else: # LÍNEAS INTERMEDIAS: solo alinear a la derecha sin timestamp
                            padding = " " * max(0, PAD_WIDTH - line_width)
                            formatted_lines.append(("", MARGIN + padding + line + "\n"))
                            current_lines += 1
                else: # Mensaje de una sola línea
                    text_width = self.visual_len(text)  # Ancho total del mensaje
                    if text_width + time_width + 3 <= PAD_WIDTH: # ¿Cabe texto + timestamp en una línea?
                        padding = " " * max(0, PAD_WIDTH - text_width - time_width - 3) # SÍ CABE: todo en una línea, alineado a la derecha
                        formatted_lines.append(("", MARGIN + padding))  # Padding izquierdo
                        formatted_lines.append(("class:msg-sent", text))  # Mensaje
                        formatted_lines.append(("class:time", f"   {formatted_time} "))  # Timestamp
                        tick_style = "class:tick-read" if status == 'delivered' else "class:tick-sent"
                        formatted_lines.append((tick_style, f"{tick}\n"))  # Tick
                        current_lines += 1
                    else: # NO CABE: mensaje muy largo, timestamp en línea separada
                        padding = " " * max(0, PAD_WIDTH - text_width)
                        formatted_lines.append(("", MARGIN + padding))
                        formatted_lines.append(("class:msg-sent", text + "\n"))
                        current_lines += 1
                        time_padding = " " * max(0, PAD_WIDTH - time_width)# Nueva línea solo para timestamp (alineado a la derecha)
                        formatted_lines.append(("", MARGIN + time_padding))
                        formatted_lines.append(("class:time", f"{formatted_time} "))
                        tick_style = "class:tick-read" if status == 'delivered' else "class:tick-sent"
                        formatted_lines.append((tick_style, f"{tick}\n"))
                        current_lines += 1
        
        formatted_lines.append(("", "\n"))  # Margen inferior para que el chat no esté pegado al borde
        current_lines += 1
        
        self._last_line_count = current_lines  # Guardamos el número total de líneas para el scroll
        return formatted_lines  # Devolvemos la lista de tuplas (estilo, texto)
    
    def get_my_account_content(self):  # Genera el contenido de la pestaña "Mi Cuenta"
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
    
    def get_help_content(self):  # Genera el contenido de la pestaña "Ayuda"
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

    def format_timestamp(self, time_str, full_date_str=None):  # Formatea timestamps de forma amigable
        try:
            if full_date_str:
                msg_datetime = datetime.strptime(full_date_str, "%Y-%m-%d %H:%M")
            else:
                msg_datetime = datetime.strptime(f"{datetime.now().strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %H:%M")
            
            now = datetime.now()
            today = now.date()
            msg_date = msg_datetime.date()

            if msg_date == today:  # Mensaje de hoy
                return f"Hoy {time_str}"
            elif msg_date == today - timedelta(days=1):  # Mensaje de ayer
                return f"Ayer {time_str}"
            elif msg_date.year == today.year:  # Mensaje de este año (pero no hoy ni ayer)
                return msg_datetime.strftime(f"%d %b {time_str}")
            else:  # Mensaje de años anteriores
                return msg_datetime.strftime(f"%d/%m/%y {time_str}")
        except:  # Si falla el parseo, devolver el string original
            return time_str

    def visual_len(self, text):  # Calcula el ancho visual real de un texto
        width = 0
        for char in text:  # Analizamos carácter por carácter
            ea = unicodedata.east_asian_width(char)  # Obtenemos la categoría del carácter
            if ea in ('F', 'W'):  # F = Fullwidth, W = Wide (emojis, caracteres CJK)
                width += 2  # Estos caracteres ocupan 2 espacios en terminal
            elif ea in ('Na', 'H', 'N', 'A'):  # Caracteres normales (ASCII, letras latinas)
                width += 1  # Estos ocupan 1 espacio
            else:
                width += 1  # Caso por defecto
        return width

    def update_ascii_suggestions(self):  # Actualiza las sugerencias de ASCII Art en tiempo real
        current_text = self.w_ascii.text.strip().lower()  # Obtenemos el texto actual (minúsculas, sin espacios)
        if not current_text:  
            self.w_suggestions.text = ""
            return
        
        matches = [key for key in self.ascii_art.keys() if current_text in key.lower()] # Buscamos claves de ASCII Art que contengan el texto escrito
        
        if matches:  # Si hay coincidencias, mostramos las primeras 5
            suggestions_text = "  Sugerencias: " + ", ".join(matches[:5])
            self.w_suggestions.text = suggestions_text
        else:  # Si no hay coincidencias, mostramos mensaje
            self.w_suggestions.text = "  Sin coincidencias"

    def refresh_ui(self):  # Actualiza la interfaz de usuario
        lines = []  # Lista de líneas de texto para el panel de contactos
        special_contacts = ["__AYUDA__", "__MI_CUENTA__"]  # Pestañas de sistema
        
        for special in special_contacts: # Asignar icono y nombre de visualización
            if special == "__AYUDA__":
                icon = "❓"  
                display = "Ayuda"
            elif special == "__MI_CUENTA__":
                icon = "👤" 
                display = "Mi Cuenta"
            
            prefix = "➞ " if self.current_cn == special else "  " # Marcar con flecha (➞) si es el contacto seleccionado actualmente
            lines.append(f"{prefix}{icon} {display}")
        
        if self.contact_keys:  # Separador entre pestañas especiales y contactos reales
            lines.append("")  
            lines.append("─" * 32)  
            lines.append("") 
        
        for k in self.contact_keys:  # Iteramos por cada contacto
            info = self.db.get_contact_info(k)  # Obtenemos info de la base de datos
            if not info:  # Si no hay info, saltamos este contacto
                continue
            
            if info.get("is_connected"):  # Conexión activa (sesión Noise IK abierta)
                icon = "🟢"
            elif info.get("session_key"):  # Hay sesión guardada pero no conectado ahora
                icon = "🔴" 
            else:  # No hay sesión guardada
                icon = "🟡"
            
            prefix = "➞ " if k == self.current_cn else "  " # Marcar contacto actual con flecha
            
            full_name = info.get("name", k) # Nombre completo desde la base de datos
            name_parts = full_name.split()
            if len(name_parts) >= 2:  # Extraer nombre + primer apellido
                if "," in full_name:  # Formato certificado: "APELLIDOS, NOMBRE"
                    parts = full_name.split(",")
                    apellidos = parts[0].strip().split()
                    nombre = parts[1].strip().split()[0] if len(parts) > 1 else ""
                    display_name = f"{nombre} {apellidos[0]}"
                else:  # Formato normal: "NOMBRE APELLIDO"
                    display_name = f"{name_parts[0]} {name_parts[1]}"
            else:
                display_name = full_name

            unread = self.db.get_unread_count(k, self.my_nick)  # Cuenta mensajes sin leer
            if unread > 0:  # Si hay mensajes sin leer, mostrar campana 🔔
                lines.append(f"{prefix}{icon} {display_name} 🔔({unread})")
            else:  # Sin mensajes pendientes
                lines.append(f"{prefix}{icon} {display_name}")
        
        self.w_contacts.text = "\n".join(lines)  # Unimos todas las líneas con saltos de línea
        self.app.invalidate()  # Forzamos redibujado de la aplicación prompt_toolkit

    def move_selection(self, delta):  # Navega entre contactos (arriba/abajo)
        all_items = ["__AYUDA__", "__MI_CUENTA__"] + self.contact_keys # Crear lista completa: pestañas especiales + contactos reales
        
        if not all_items:  # Si no hay nada, no hacer nada
            return
        
        idx = all_items.index(self.current_cn) if self.current_cn in all_items else 0 # Obtener índice actual del contacto seleccionado
        new_idx = (idx + delta) % len(all_items) # Calcular nuevo índice (con wrap-around: si llegamos al final, volvemos al principio)
        self.current_cn = all_items[new_idx] # Actualizar contacto actual
        self.scroll_offset = 0 # Resetear scroll al cambiar de contacto

        if self.current_cn not in ["__AYUDA__", "__MI_CUENTA__"]: # Si cambiamos a un contacto, marcar mensajes como leídos
            self.db.mark_messages_as_read(self.current_cn, self.my_nick)
        
        self.refresh_ui()  # Actualizar interfaz para reflejar el cambio

    def add_peer(self, name, ip, port):  # Añade un nuevo contacto a la lista evitando duplicados
        
        existing_cn = None  # Identificador de contacto existente
        all_contacts = self.db.get_all_contacts()  # Obtenemos todos los contactos de la BD
        
        for cn, info in all_contacts.items(): # Primera búsqueda: por IP y puerto
            if info.get("ip") == ip and info.get("port") == port:  # Coincidencia exacta
                existing_cn = cn  # Encontramos un contacto con la misma IP:puerto
                break
        if not existing_cn: # Segunda búsqueda: por nombre
            target_name = name.strip().lower()  # Normalizamos el nombre (minúsculas, sin espacios)
            for cn, info in all_contacts.items():
                db_name = info.get("name", "").strip().lower()  # Normalizamos nombre en BD
                if db_name == target_name:  # Coincidencia de nombre
                    existing_cn = cn
                    break

        if existing_cn:  # Si encontramos un contacto existente
            contact_id = existing_cn
            self.db.add_or_update_contact(existing_cn, name=name, ip=ip, port=port)  # Actualizamos la información (por si cambió IP, puerto, etc.)
        else:  # Si es un contacto completamente nuevo
            contact_id = f"{ip}:{port}"  # Usamos "IP:puerto" como identificador
            self.db.add_or_update_contact(contact_id, name=name, ip=ip, port=port)  # Creamos en BD
            if contact_id not in self.contact_keys:  # Si no está en la lista de UI lo añadimos
                self.contact_keys.append(contact_id)  
                self.contact_keys.sort()  
            if not self.current_cn:  # Si no hay contacto seleccionado aún
                self.current_cn = contact_id
        
        self.refresh_ui()  # Actualizamos la interfaz para mostrar el nuevo/actualizado contacto

    def on_protocol_msg(self, addr, text, real_cn, msg_id=None):  # Callback del protocolo: maneja eventos de red
        if text == "SESSIONS_READY":  # Señal de que protocol.py cargó todas las sesiones de la BD
            asyncio.create_task(self.auto_connect_and_send_all())  # Intentar reconectar con todos
            return
        
        if addr is None:  # Si no hay dirección, no podemos procesar el evento
            return

        contact_id = None
        all_contacts = self.db.get_all_contacts()
        
        
        for cn, info in all_contacts.items(): # Búsqueda 1: Por dirección IP y puerto
            if info.get("ip") == addr[0] and info.get("port") == addr[1]:
                contact_id = cn
                break
        if not contact_id: # Búsqueda 2: Por nombre
            for cn, info in all_contacts.items():
                if info.get("name") == real_cn:
                    contact_id = cn
                    break
        if not contact_id: # Búsqueda 3: Si no existe, usar el nombre real como identificador
            contact_id = real_cn
        if contact_id in self.pending_handshakes: # Si este contacto tenía un handshake pendiente, quitarlo del conjunto
            self.pending_handshakes.discard(contact_id)  # Ya no está "conectando..."
        
        if contact_id in all_contacts: # Actualizar o crear el contacto en la base de datos Si ya existía
            self.db.add_or_update_contact(contact_id, ip=addr[0], port=addr[1])  # Actualizar IP/puerto
        else:  # Si es nuevo
            self.db.add_or_update_contact(contact_id, name=real_cn, ip=addr[0], port=addr[1])  # Crear nuevo
        
        if contact_id not in self.contact_keys: # Añadir a la lista de contactos de la UI si no está
            self.contact_keys.append(contact_id)
            self.contact_keys.sort()
        
        ts = datetime.now().strftime("%H:%M")  # Timestamp para mensajes del sistema
        
        if text in ["HANDSHAKE_OK_INIT", "HANDSHAKE_OK_RESP"]: # HANDSHAKE COMPLETADO
            self.db.set_contact_connected(contact_id, True) 
            msgs = self.db.get_history(contact_id) 
            user_msgs = [m for m in msgs if m.get('sender') != "Sys"]
            if len(user_msgs) == 0:  # Primera vez que hablamos con este contacto
                self.db.add_message(contact_id, "Sys", "🔒 Conexión segura establecida", "system", ts)
            
            self.protocol.enviar_pending_send(addr[0], addr[1]) # Avisar que vamos a enviar mensajes pendientes
            self.send_pending_messages(contact_id, addr[0], addr[1], lambda: self.protocol.enviar_pending_done(addr[0], addr[1])) # Enviar todos los mensajes que quedaron pendientes mientras estábamos desconectados
            self.pending_sent[(addr[0], addr[1])] = True # Marcar que ya enviamos nuestros pendientes (para evitar duplicados)
        
        elif text == "SESSION_RESTORED_INIT": # SESIÓN RESTAURADA Iniciar
            self.db.set_contact_connected(contact_id, True)
            self.protocol.enviar_pending_send(addr[0], addr[1]) # Enviar pendientes
            self.send_pending_messages(contact_id, addr[0], addr[1], lambda: self.protocol.enviar_pending_done(addr[0], addr[1]))
            self.pending_sent[(addr[0], addr[1])] = True
        
        elif text == "SESSION_RESTORED_RESP": # SESIÓN RESTAURADA Responder
            self.db.set_contact_connected(contact_id, True)
            self.pending_sent[(addr[0], addr[1])] = False  # Esperamos a que el iniciador envíe sus pendientes primero
        
        elif text == "PEER_SENDING_PENDING": # Nos indica que va a enviarnos mensajes pendientes
            pass  # Solo informativo, no hacemos nada
        
        elif text == "SEND_MY_PENDING": # Nos indica que quiere que le enviemos nuestros mensajes pendientes
            if not self.pending_sent.get((addr[0], addr[1]), False): # Solo enviar si aún no lo hemos hecho (evitar duplicados)
                self.pending_sent[(addr[0], addr[1])] = True
                self.protocol.enviar_pending_send(addr[0], addr[1])
                self.send_pending_messages(contact_id, addr[0], addr[1], lambda: self.protocol.enviar_pending_done(addr[0], addr[1]))
        
        elif text == "RECONNECT_TIMEOUT": # Timeout de reconexión
            self.db.set_contact_connected(contact_id, False)  # Marcar como desconectado
            self.refresh_ui() 
            return  # No procesamos más, terminamos aquí
        
        elif text.startswith("HANDSHAKE_ERROR"): # Error en el Handshake
            self.db.set_contact_connected(contact_id, False)  # Marcar como desconectado
        
        
        elif text == "ERROR_DESCIFRADO": # Error de descifrado (mensaje corrupto o clave incorrecta)
            pass  # Solo informativo, el protocolo ya maneja el error
        
        elif text.startswith("ACK|"): # ACK recibido 
            ack_msg_id = text.split('|', 1)[1]  # Extraemos el ID del mensaje confirmado
            self.db.mark_message_status(contact_id, ack_msg_id, "delivered") # Actualizar estado del mensaje: 🕒 (sent) -> ✅ (delivered)
        
        else: # Mensaje normal recibido
            self.db.set_contact_connected(contact_id, True)  # Asegurar que está marcado como conectado
            received_msg_id = self.db.add_message(contact_id, real_cn, text, "received", ts, msg_id=msg_id) # Añadir mensaje al historial con estado "received"
            if self.current_cn == contact_id: # Si estamos viendo el chat con este contacto, marcar como leído inmediatamente
                self.db.mark_message_as_read_by_id(contact_id, received_msg_id)
        
        self.refresh_ui()  # Actualizar interfaz para reflejar cambios

    def send_pending_messages(self, cn, ip, port, callback=None): # Envía todos los mensajes pendientes de un contacto (tras reconectar o handshake)
        pending = self.db.get_pending_messages(cn)  # Obtener mensajes con estado "pending"
        
        if not pending or cn in self.sending_pending: # Si no hay mensajes pendientes O ya estamos enviando para este contacto, salir
            if callback:  # Si hay callback (normalmente enviar_pending_done), ejecutarlo
                callback()
            return
        
        
        if not self.protocol.tiene_sesion(ip, port): # Verificar que hay sesión activa antes de enviar
            return 
        
        
        self.sending_pending.add(cn) # Añadir contacto al conjunto de "enviando pendientes" (evita re-entrada)
        
        async def send_all_async(): # Función asíncrona interna que envía mensajes uno por uno, el delay entre mensajes es importante para no saturar la red
            
            for msg in pending:  # Iteramos por cada mensaje pendiente
                self.protocol.enviar_mensaje(ip, port, msg['text'], msg['id']) # Enviar mensaje por la red
                self.db.mark_message_status(cn, msg['id'], "sent") # Actualizar estado en BD

                await asyncio.sleep(0.2) # Delay crítico: da tiempo al event loop para procesar otros eventos. Sin esto, la UI se congelaría durante el envío masivo
                
                self.refresh_ui()
            
            self.sending_pending.discard(cn)
            self.refresh_ui()
            
            if callback: # Ejecutar callback si existe (normalmente enviar_pending_done)
                callback()

        asyncio.create_task(send_all_async()) # Lanzar la tarea asíncrona (no bloqueante)

    async def handle_enter(self):  # Maneja la pulsación de Enter
        
        
        if self.current_cn in ["__AYUDA__", "__MI_CUENTA__"]: # Si estamos en pestañas de ayuda o cuenta, Enter no hace nada
            return
        
        # === CASO 1: ENVÍO DE ASCII ART (si el foco está en w_ascii) ===
        if self.app.layout.has_focus(self.w_ascii):
            ascii_key = self.w_ascii.text.strip()  # Obtener clave escrita
            self.w_ascii.text = ""  # Limpiar campo inmediatamente
            
            if ascii_key in self.ascii_art:  # Si la clave existe en el diccionario
                ascii_text = self.ascii_art[ascii_key]  # Obtener el dibujo ASCII
                
                # Validaciones básicas
                if not self.current_cn:
                    return
                info = self.db.get_contact_info(self.current_cn)
                if not info:
                    return
                
                ip, port = info.get("ip"), info.get("port")
                ts = datetime.now().strftime("%H:%M")
                
                # Si no hay sesión, guardar como pendiente e iniciar handshake
                if not ip or not self.protocol.tiene_sesion(ip, port):
                    self.db.add_message(self.current_cn, self.my_nick, ascii_text, "pending", ts)
                    if ip and port:  # Si tenemos IP, intentar conectar
                        self.protocol.enviar_handshake(ip, port, cn=self.current_cn)
                    self.refresh_ui()
                    return
                
                # Si hay sesión, enviar inmediatamente
                msg_id = self.db.add_message(self.current_cn, self.my_nick, ascii_text, "sent", ts)
                if not self.protocol.enviar_mensaje(ip, port, ascii_text, msg_id):
                    # Si falla el envío, marcar como pendiente y cerrar sesión
                    self.db.mark_message_status(self.current_cn, msg_id, "pending")
                    self.db.set_contact_connected(self.current_cn, False)
                    self.protocol.cerrar_sesion(ip, port)
                self.refresh_ui()
            return  # Terminamos aquí (caso ASCII)
        
        # === CASO 2: ENVÍO DE MENSAJE NORMAL (si el foco está en w_input) ===
        text = self.w_input.text.strip()  # Obtener texto escrito
        
        # Validaciones
        if not self.current_cn:  # Sin contacto seleccionado
            self.w_input.text = ""
            return
        
        info = self.db.get_contact_info(self.current_cn)
        if not info:  # Contacto no encontrado en BD (no debería pasar)
            return
        
        ip, port = info.get("ip"), info.get("port")
        ts = datetime.now().strftime("%H:%M")
        
        # Si el contacto no tiene IP (offline), mostrar error
        if not ip:
            if text:  # Solo si el usuario escribió algo
                self.db.add_message(self.current_cn, "Sys", "Usuario Offline - Sin IP", "error", ts)
            self.w_input.text = ""
            self.refresh_ui()
            return
        
        # Si no hay sesión activa, guardar mensaje como pendiente e iniciar handshake
        if not self.protocol.tiene_sesion(ip, port):
            if text:  # Solo si hay texto (Enter vacío solo conecta sin enviar)
                self.db.add_message(self.current_cn, self.my_nick, text, "pending", ts)
                self.scroll_offset = 0  # Resetear scroll (mostrar mensajes recientes)
                self.w_input.text = ""  # Limpiar campo de entrada
            
            # Iniciar handshake si no está ya en progreso
            if self.current_cn not in self.pending_handshakes:
                self.protocol.enviar_handshake(ip, port, cn=self.current_cn)
                self.pending_handshakes.add(self.current_cn)  # Marcar como "conectando..."
                self.refresh_ui()
            return
        
        # Si hay texto Y hay sesión, enviar inmediatamente
        if text:
            msg_id = self.db.add_message(self.current_cn, self.my_nick, text, "sent", ts)
            self.scroll_offset = 0  # Resetear scroll
            self.w_input.text = ""  # Limpiar campo
            
            # Intentar enviar el mensaje por la red
            if not self.protocol.enviar_mensaje(ip, port, text, msg_id):
                # Si falla el envío, marcar como pendiente y cerrar sesión
                self.db.mark_message_status(self.current_cn, msg_id, "pending")
                self.db.set_contact_connected(self.current_cn, False)
                self.protocol.cerrar_sesion(ip, port)
            
            self.refresh_ui()  # Actualizar UI

    def force_disconnect(self):  # Fuerza la desconexión manual del contacto actual (Ctrl+D)
        # Útil para cerrar sesión voluntariamente o simular desconexión
        
        if not self.current_cn:  # Sin contacto seleccionado, no hacer nada
            return
        
        info = self.db.get_contact_info(self.current_cn)
        if info and info.get("ip"):  # Si el contacto tiene IP
            # Cerrar sesión en el protocolo (elimina cipher states, claves de sesión, etc.)
            self.protocol.cerrar_sesion(info["ip"], info["port"])
            # Marcar como desconectado en BD
            self.db.set_contact_connected(self.current_cn, False)
            # Añadir mensaje de sistema informativo
            ts = datetime.now().strftime("%H:%M")
            self.db.add_message(self.current_cn, "Sys", "Desconectado manualmente", "system", ts)
            self.refresh_ui()  # Actualizar UI (icono cambia a rojo 🔴)

    async def auto_connect_and_send_all(self):  # Reconecta automáticamente con todos los contactos al iniciar
        # Esta función se ejecuta una vez al arrancar la aplicación (tras cargar sesiones de BD)
        # Intenta restablecer todas las sesiones guardadas previamente
        
        await asyncio.sleep(0.5)  # Delay inicial para dar tiempo a que todo se inicialice
        
        # Obtener todos los contactos de la BD
        all_contacts = list(self.db.get_all_contacts().items())
        
        # Intentar handshake con cada contacto (solo si tienen IP y puerto)
        for cn, info in all_contacts:
            ip = info.get("ip")
            port = info.get("port")
            if not ip or not port:  # Si no hay IP/puerto, saltar
                continue
            
            # Enviar handshake (puede ser nuevo o restaurar sesión guardada)
            self.protocol.enviar_handshake(ip, port, cn=cn)
            await asyncio.sleep(0.1)  # Pequeño delay entre handshakes (no saturar la red)
        
        self.refresh_ui()  # Actualizar UI

    async def monitor_window_size(self):  # Monitorea cambios en el tamaño de la ventana terminal
        # Cuando el usuario redimensiona la terminal, debemos recalcular los anchos para alineación
        # Esta tarea se ejecuta en bucle infinito cada 100ms
        
        while True:
            await asyncio.sleep(0.1)  # Verificar cada 100ms
            try:
                if self.w_chat_window.render_info:  # Si hay info de renderizado disponible
                    current_width = self.w_chat_window.render_info.window_width
                    # Si el ancho cambió, forzar redibujado
                    if current_width != self._last_window_width and current_width > 0:
                        self._last_window_width = current_width
                        if self.app:
                            self.app.invalidate()  # Redibuja toda la interfaz
            except:
                pass  # Ignorar errores (puede que render_info no esté disponible aún)
    
    async def force_ui_refresh(self):  # Fuerza redibujado constante de la UI
        # Esta tarea evita que la UI se congele o no responda a eventos
        # En Windows especialmente, prompt_toolkit puede "dormirse" sin esto
        
        while True:
            await asyncio.sleep(0.05)  # Cada 50ms (20 veces por segundo)
            try:
                if self.app:
                    self.app.invalidate()  # Forzar redibujado completo
            except:
                pass  # Ignorar errores

    async def check_ack_timeouts(self):  # Comprueba timeouts de ACKs no recibidos
        # Esta tarea verifica si algún mensaje enviado no recibió ACK en tiempo razonable
        # Si un mensaje queda sin ACK mucho tiempo, asumimos que la conexión se perdió
        
        while True:
            await asyncio.sleep(0.5)  # Verificar cada 500ms
            
            for cn in list(self.contact_keys):  # Iterar por cada contacto
                # Si estamos enviando pendientes, no verificar timeouts (esperado que tarde)
                if cn in self.sending_pending:
                    continue
                
                info = self.db.get_contact_info(cn)
                if info and info.get("is_connected"):  # Solo verificar si está marcado como conectado
                    # Verificar si hay mensajes con timeout (sin ACK en >0.5s)
                    has_timeout = self.db.check_message_timeouts(cn, timeout_seconds=0.5)
                    
                    if has_timeout:  # Si hay timeout, asumir desconexión
                        self.db.set_contact_connected(cn, False)  # Marcar como desconectado
                        ip = info.get("ip")
                        port = info.get("port")
                        if ip and port:
                            self.protocol.cerrar_sesion(ip, port)  # Cerrar sesión en protocolo
                        
                        # Todos los mensajes "sent" vuelven a "pending" (se reenviarán al reconectar)
                        msgs = self.db.get_history(cn)
                        for msg in msgs:
                            if msg.get("status") == "sent":  # 🕒 -> pendiente
                                self.db.mark_message_status(cn, msg["id"], "pending")
                        
                        self.refresh_ui()  # Actualizar UI (icono rojo 🔴)

    async def _keep_loop_awake(self):  # Tarea especial para Windows: mantener el event loop activo
        # === PROBLEMA DE WINDOWS CON PROMPT_TOOLKIT ===
        # En Windows, el event loop de asyncio puede "dormirse" mientras espera input del usuario
        # Esto causa que las tareas asíncronas (envío de mensajes, ACKs, redibujado) se bloqueen
        # hasta que el usuario presiona una tecla (especialmente Enter)
        #
        # === SOLUCIÓN: WAKEUP PACKETS ===
        # Enviamos paquetes UDP a nosotros mismos constantemente (cada 100ms)
        # Esto genera eventos de I/O que fuerzan al event loop a despertarse y procesar todas
        # las tareas pendientes (envíos, ACKs, redibujado de UI, etc.)
        #
        # Sin esta tarea, los mensajes solo se enviarían al presionar Enter otra vez
        
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # Socket UDP
        sock.bind(('127.0.0.1', 0))  # Bind a localhost en puerto aleatorio
        addr = sock.getsockname()  # Obtener dirección (localhost, puerto_aleatorio)
        sock.setblocking(False)  # No bloquear en recv (importante para asyncio)
        
        while True:
            await asyncio.sleep(0.1)  # Cada 100ms
            try:
                # Enviar paquete a nosotros mismos (genera evento de I/O)
                sock.sendto(b'wake', addr)
                
                # Leer para vaciar el buffer (evitar acumulación)
                try:
                    sock.recv(1024)  # Intentar recibir
                except BlockingIOError:  # Es normal que falle (socket no bloqueante)
                    pass
            except:
                pass  # Ignorar cualquier error (no crítico)

    async def run(self):  # Función principal que ejecuta la TUI y todas las tareas en background
        # Esta función es el punto de entrada de la TUI después de la inicialización
        # Lanza todas las tareas asíncronas necesarias y ejecuta la aplicación prompt_toolkit
        
        # === LANZAMIENTO DE TAREAS EN BACKGROUND ===
        # Todas estas tareas se ejecutan concurrentemente en el event loop
        
        self._timeout_check_task = asyncio.create_task(self.check_ack_timeouts())  # Verificar timeouts de ACKs
        self._reconnect_timeout_task = asyncio.create_task(self.protocol.check_reconnect_timeouts())  # Verificar timeouts de reconexión (protocol.py)
        self._window_monitor_task = asyncio.create_task(self.monitor_window_size())  # Monitorear redimensionado de terminal
        self._ui_refresh_task = asyncio.create_task(self.force_ui_refresh())  # Refrescar UI constantemente
        self._wakeup_task = asyncio.create_task(self._keep_loop_awake())  # Mantener event loop despierto (Windows fix)
        
        try:
            # Ejecutar la aplicación prompt_toolkit (bloquea hasta que el usuario salga con Ctrl+C)
            await self.app.run_async()
        finally:
            # === LIMPIEZA AL SALIR ===
            # Cuando el usuario cierra la aplicación, debemos cancelar todas las tareas en background
            # Si no hacemos esto, el programa no terminará correctamente
            
            for task in [self._timeout_check_task, self._reconnect_timeout_task, 
                         self._window_monitor_task, self._ui_refresh_task, self._wakeup_task]:
                if task:  # Si la tarea fue creada
                    task.cancel()  # Solicitar cancelación
                    try:
                        await task  # Esperar a que la tarea termine
                    except asyncio.CancelledError:  # Es normal que lance esta excepción
                        pass  # Ignorar (comportamiento esperado)
