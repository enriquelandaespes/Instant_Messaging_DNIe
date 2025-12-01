import asyncio
import struct
import os
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.backends import default_backend
import config

# Definición de los diferentes tipos de paquetes que tenemos para los diferentes flujos del protocolo
# Cada constante representa un byte identificador único para el tipo de mensaje
PKT_EPHEMERAL_KEY = 0x01   # Fase 1 Handshake: Intercambio de clave pública efímera (X25519)
PKT_MSG = 0x02             # Mensaje de chat cifrado (Payload de aplicación)
PKT_ACK = 0x04             # Confirmación de recepción (Acknowledge)
PKT_RECONNECT_REQ = 0x05   # Solicitud de reconexión rápida (Session Resumption)
PKT_RECONNECT_RESP = 0x06  # Respuesta de reconexión aceptada
PKT_PENDING_SEND = 0x07    # Señalización: "Voy a empezar a enviar mensajes pendientes"
PKT_PENDING_DONE = 0x08    # Señalización: "He terminado de enviar mensajes pendientes"
PKT_HANDSHAKE_INIT = 0x10  # Fase 2 Handshake: Certificado cifrado del Iniciador
PKT_HANDSHAKE_RESP = 0x11  # Fase 2 Handshake: Certificado cifrado del Responder

class SecureIMProtocol(asyncio.DatagramProtocol): 

    # Clase que implementa el protocolo de mensajería segura sobre UDP.

    def __init__(self, dnie_manager, db, on_msg_callback):
        self.dnie = dnie_manager
        self.db = db
        self.transport = None
        self.callback = on_msg_callback
        self.sessions = {} # Almacena el estado de las sesiones activas {addr: {cipher, name, state}}
        self.my_cid = os.urandom(4) # Identificador de conexión aleatorio para evitar ataques de Spoofing
        self.reconnect_pending = {} # Control de timeouts para intentos de reconexión
        self.role = {} # Rol en la conexión: 'initiator' o 'responder' (importante para el orden de envío)
        self.pending_sent = {} # Estado de sincronización de mensajes pendientes
        self.ephemeral_keys = {} # Almacenamiento temporal para el intercambio Diffie-Hellman efímero

    def connection_made(self, transport): 
        # Callback de asyncio cuando el socket UDP está listo para transmitir
        self.transport = transport
        if self.callback:
            self.callback(None, "SESSIONS_READY", "System", None)

    def datagram_received(self, data, addr): 
        # Callback principal de recepción de paquetes UDP despacha el paquete a la función manejadora correspondiente según su tipo (primer byte).
        if len(data) < 5: # Descartar paquetes malformados o demasiado cortos
            return
        msg_type = data[0]
        payload = data[5:] # El payload comienza después del tipo (1 byte) y el CID (4 bytes)
        
        self.touch_session(addr) # Actualizar timestamp de actividad 
        
        # Máquina de estados para procesar cada tipo de paquete
        if msg_type == PKT_EPHEMERAL_KEY:
            self.handle_ephemeral_key(payload, addr)
        elif msg_type == PKT_HANDSHAKE_INIT:
            asyncio.create_task(self.handle_handshake(payload, addr, is_response=False))
        elif msg_type == PKT_HANDSHAKE_RESP:
            asyncio.create_task(self.handle_handshake(payload, addr, is_response=True))
        elif msg_type == PKT_MSG:
            self.handle_message(payload, addr)
        elif msg_type == PKT_ACK:
            self.handle_ack(payload, addr)
        elif msg_type == PKT_RECONNECT_REQ:
            asyncio.create_task(self.handle_reconnect_req(payload, addr))
        elif msg_type == PKT_RECONNECT_RESP:
            asyncio.create_task(self.handle_reconnect_resp(payload, addr))
        elif msg_type == PKT_PENDING_SEND:
            asyncio.create_task(self.handle_pending_send(payload, addr))
        elif msg_type == PKT_PENDING_DONE:
            self.handle_pending_done(payload, addr)

    def touch_session(self, addr): 
        # Actualiza el timestamp de la sesión para evitar timeouts durante la negociación
        if addr in self.reconnect_pending:
            self.reconnect_pending[addr]['timestamp'] = asyncio.get_event_loop().time()

    def handle_ephemeral_key(self, payload, addr): 
        # Fase 1 del Handshake: Procesamiento de la clave pública efímera (ECDH).
        # Establece un canal cifrado temporal para transmitir el certificado de forma segura.
        try:
            if len(payload) < 32: # Validación: Clave pública X25519 debe ser de 32 bytes
                return
            
            peer_ephemeral_pub_bytes = payload[:32]
            
            # Determinar rol en el intercambio Diffie-Hellman
            is_initiator = addr in self.ephemeral_keys
            
            if not is_initiator:  # Si soy el Responder, debo generar mi par de claves efímeras ahora
                my_ephemeral_private = x25519.X25519PrivateKey.generate() # Generación de clave privada usando curva elíptica X25519
                self.ephemeral_keys[addr] = {
                    'private': my_ephemeral_private,
                    'public_bytes': my_ephemeral_private.public_key().public_bytes_raw()
                }
                # Responder envía su clave pública efímera de vuelta al Iniciador
                # "B" indica unsigned char (1 byte) para el tipo de paquete
                packet = struct.pack("B", PKT_EPHEMERAL_KEY) + self.my_cid + self.ephemeral_keys[addr]['public_bytes']
                self.transport.sendto(packet, addr)
            
            # CÁLCULO DEL SECRETO COMPARTIDO (ECDH) 
            # 1. Recuperamos la clave pública del peer desde los bytes recibidos
            peer_ephemeral_key = x25519.X25519PublicKey.from_public_bytes(peer_ephemeral_pub_bytes)
            
            # 2. Realizamos el intercambio Diffie-Hellman: (Privada_Mía * Pública_Peer)
            ephemeral_shared = self.ephemeral_keys[addr]['private'].exchange(peer_ephemeral_key)
            
            # 3. Derivación de clave: Hash Blake2s del secreto compartido para obtener clave simétrica de 256 bits
            temp_key = hashlib.blake2s(ephemeral_shared, digest_size=32).digest()
            
            # 4. Inicialización del cifrador AEAD (ChaCha20-Poly1305) con la clave derivada
            # Este canal cifrado temporal protege el certificado contra observadores pasivos
            self.ephemeral_keys[addr]['temp_cipher'] = ChaCha20Poly1305(temp_key)
            self.ephemeral_keys[addr]['peer_public'] = peer_ephemeral_pub_bytes
            
            # Fase 2: Enviar credenciales (Certificado X.509) cifradas por el canal temporal
            if is_initiator:
                # El Iniciador envía primero su certificado
                self.enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_INIT)
            else:
                # El Responder envía su certificado después
                self.enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
            
        except Exception:
            pass
    
    async def handle_handshake(self, payload, addr, is_response): 
        # Fase 2 del Handshake: Verificación de identidad y establecimiento de sesión permanente.
        # Descifra el certificado, valida el DNIe y establece la clave de sesión final.
        if addr in self.sessions: # Ignorar si la sesión ya está establecida (idempotencia)
            return
        
        # Seguridad: Verificar que existe un contexto criptográfico efímero previo
        if addr not in self.ephemeral_keys or 'temp_cipher' not in self.ephemeral_keys[addr]:
            return
        
        try:
            temp_cipher = self.ephemeral_keys[addr]['temp_cipher']
            offset = 0
            
            if len(payload) < 44:  # Validación longitud mínima: 32 (pub key) + 12 (nonce)
                return
            
            # Extracción de componentes del paquete
            peer_pub_bytes = payload[offset:offset+32] # Clave pública permanente del DNIe
            offset += 32
            
            nonce = payload[offset:offset+12] # Nonce único usado para cifrar el certificado
            offset += 12
            
            encrypted_cert = payload[offset:] # Certificado cifrado + Tag de autenticación
            
            # DESCIFRADO Y AUTENTICACIÓN 
            try:
                # Decrypt verifica la integridad y descifra. Si falla, lanza excepción.
                cert_bytes = temp_cipher.decrypt(nonce, encrypted_cert, None)
            except Exception:
                # Fallo de autenticación: posible ataque Man-in-the-Middle o corrupción
                if addr in self.ephemeral_keys:
                    del self.ephemeral_keys[addr]
                return
            
            #  PARSEO DEL CERTIFICADO X.509 
            try:
                cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
                # Extracción del Common Name (CN) que contiene el nombre del titular del DNI
                cn_attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                if cn_attrs:
                    raw = str(cn_attrs[0].value)
                    # Limpieza del string para obtener solo el nombre legible
                    nombre = raw.replace("(AUTENTICACIÓN)", "").replace("(Autenticación)", "").replace("(FIRMA)", "").replace("(Firma)", "").strip()
                else:
                    nombre = "DNIe Desconocido"
            except:
                nombre = "Error Certificado"
            
            # SEGUNDO INTERCAMBIO DIFFIE-HELLMAN (Claves Permanentes)
            # Usamos las claves del DNIe para derivar la clave de sesión final.
            # Esto garantiza autenticación mutua criptográfica.
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            shared_secret = self.dnie.private_key.exchange(peer_key_obj)
            session_key = hashlib.blake2s(shared_secret, digest_size=32).digest() # KDF final
            
            # Establecimiento de la sesión segura
            self.sessions[addr] = {
                'cipher': ChaCha20Poly1305(session_key), # Cifrador para tráfico de datos
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            
            # Actualización de la base de datos de contactos
            existing_cn = None
            all_contacts = self.db.get_all_contacts()
            for cn, info in all_contacts.items():
                if info.get("ip") == addr[0] and info.get("port") == addr[1]:
                    existing_cn = cn
                    break
            
            contact_id = existing_cn if existing_cn else nombre
            self.db.add_or_update_contact(
                contact_id,
                name=nombre,
                ip=addr[0],
                port=addr[1],
                session_key=session_key.hex(), # Persistencia de clave para reconexión rápida y segura sin paso de handshakes
                peer_cert=cert_bytes.hex()
            )
            
            # Determinación del rol para sincronización de mensajes
            if is_response: 
                self.role[addr] = "initiator" # Recibí respuesta -> Soy Iniciador
                cb_msg = "HANDSHAKE_OK_INIT"
            else:
                self.role[addr] = "responder" # Recibí inicio -> Soy Responder
                cb_msg = "HANDSHAKE_OK_RESP"
            
            if self.callback:
                self.callback(addr, cb_msg, nombre, None)
            
            # Limpieza de claves efímeras (Perfect Forward Secrecy para el handshake)
            if addr in self.ephemeral_keys:
                del self.ephemeral_keys[addr]
                
        except Exception:
            pass

    def handle_message(self, payload, addr): 
        """
        Procesa un mensaje de chat cifrado entrante.
        Realiza descifrado AEAD, validación de integridad y envío de ACK.
        """
        if addr not in self.sessions:
            return
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', 'Unknown')
        try:
            # Extracción del Nonce (12 bytes) y Ciphertext
            nonce = payload[:12]
            ciphertext = payload[12:]
            
            # Descifrado autenticado (AEAD)
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            msg_data = plaintext.decode('utf-8')
            
            # Separación de ID de mensaje y contenido
            if '|' in msg_data:
                msg_id, msg = msg_data.split('|', 1)
                # Enviar confirmación de recepción (ACK) inmediatamente
                self.enviar_ack(addr[0], addr[1], msg_id)
            else:
                msg_id = None
                msg = msg_data
            
            # Notificar a la capa superior (UI)
            if self.callback:
                self.callback(addr, msg, nombre, msg_id)
            
            if msg_id:
                # Ya se envió el ACK arriba, no es necesario enviarlo de nuevo
                pass
        except:
            pass

    def enviar_handshake(self, ip, port, cn=None): 
        """
        Inicia el proceso de establecimiento de conexión segura.
        Intenta primero reconexión rápida (Session Resumption) si existe clave previa.
        Si no, inicia Handshake completo (Intercambio de claves efímeras).
        """
        addr = (ip, port)

        if addr in self.sessions: # Si ya hay sesión en memoria, notificar éxito
            if self.callback:
                contact_name = self.sessions[addr].get('name', 'Unknown')
                self.callback(addr, "SESSIONS_OK", contact_name, None)
            return True
        
        # Búsqueda de sesión previa en base de datos
        saved_key = None
        contact_name = None
        
        if cn:
            contact_info = self.db.get_contact_info(cn)
            if contact_info:
                saved_key = contact_info.get("session_key")
                contact_name = contact_info.get("name", cn)
        
        if not saved_key:
            all_contacts = self.db.get_all_contacts()
            for name, info in all_contacts.items():
                if info.get("ip") == ip and info.get("port") == port:
                    saved_key = info.get("session_key")
                    if saved_key:
                        contact_name = info.get("name", name)
                        break
        
        # Intento de Reconexión Rápida (0-RTT o 1-RTT simplificado)
        if saved_key: 
            try:
                if isinstance(saved_key, str):
                    session_key = bytes.fromhex(saved_key)
                else:
                    session_key = saved_key

                # Restaurar contexto de seguridad
                self.sessions[addr] = {
                    'cipher': ChaCha20Poly1305(session_key),
                    'name': contact_name,
                    'state': 'ESTABLISHED'
                }
                
                final_cn = cn if cn else contact_name
                
                self.reconnect_pending[addr] = {
                    'cn': final_cn,
                    'timestamp': asyncio.get_event_loop().time()
                }
                
                # Enviar solicitud de reconexión
                self.enviar_reconnect_req(ip, port)
                return True
            except Exception:
                pass
        
        # Si falla reconexión o no hay clave previa -> Handshake Completo
        # Fase 1: Enviar clave efímera
        self.enviar_clave_efimera(ip, port)
        return False
    
    def enviar_clave_efimera(self, ip, port):
        """
        Genera y envía un par de claves efímeras para iniciar el Handshake.
        Esto garantiza Perfect Forward Secrecy (PFS) para la transmisión del certificado.
        """
        if not self.transport:
            return
        try:
            addr = (ip, port)
            # Generación de par de claves efímeras X25519
            my_ephemeral_private = x25519.X25519PrivateKey.generate()
            public_bytes = my_ephemeral_private.public_key().public_bytes_raw()
            
            self.ephemeral_keys[addr] = {
                'private': my_ephemeral_private,
                'public_bytes': public_bytes
            }
            
            # Construcción y envío del paquete
            packet = struct.pack("B", PKT_EPHEMERAL_KEY) + self.my_cid + public_bytes
            self.transport.sendto(packet, (ip, port))
        except Exception:
            pass

    def cerrar_sesion(self, ip, port): 
        """Limpia el estado de la sesión y estructuras asociadas."""
        addr = (ip, port)
        if addr in self.sessions:
            del self.sessions[addr]
        if addr in self.reconnect_pending:
            del self.reconnect_pending[addr]
        if addr in self.role:
            del self.role[addr]
        if addr in self.pending_sent:
            del self.pending_sent[addr]

    def tiene_sesion(self, ip, port): 
        """Verifica si existe una sesión criptográfica activa con el peer."""
        addr = (ip, port)
        return addr in self.sessions

    def enviar_paquete_credenciales(self, ip, port, tipo): 
        """
        Cifra y envía el certificado X.509 del usuario.
        Utiliza el canal efímero establecido en la Fase 1 para proteger la identidad.
        """
        if not self.transport:
            return
        addr = (ip, port)
        
        # Verificar integridad del flujo: debe existir canal efímero
        if addr not in self.ephemeral_keys or 'temp_cipher' not in self.ephemeral_keys[addr]:
            return
        
        try:
            cert, firma = self.dnie.obtener_credenciales()
            temp_cipher = self.ephemeral_keys[addr]['temp_cipher']
            
            # Generación de Nonce único (96 bits) para ChaCha20Poly1305
            # CRÍTICO: Reutilizar un nonce con la misma clave rompe totalmente la seguridad
            nonce = os.urandom(12) 
            
            # Cifrado autenticado (AEAD) del certificado
            encrypted_cert = temp_cipher.encrypt(nonce, cert, None)
            
            # Construcción del paquete: [Tipo] [CID] [PubKey_Perm] [Nonce] [Cert_Cifrado]
            packet = (
                struct.pack("B", tipo) + self.my_cid + 
                self.dnie.public_bytes + nonce + encrypted_cert
            )
            self.transport.sendto(packet, (ip, port))
        except Exception:
            pass

    def enviar_mensaje(self, ip, port, texto, msg_id=None): 
        """
        Cifra y envía un mensaje de texto al peer.
        Utiliza la clave de sesión establecida y un nonce aleatorio por mensaje.
        """
        addr = (ip, port)
        if addr not in self.sessions:
            return False
        try:
            cipher = self.sessions[addr]['cipher']
            
            # Generación de Nonce aleatorio único por mensaje
            nonce = os.urandom(12) 
            
            # Formato del payload: "ID|Texto" para control de flujo y ACKs
            msg_data = f"{msg_id}|{texto}" if msg_id else texto
            
            # Cifrado AEAD: Garantiza que solo el peer con la clave de sesión puede leerlo
            # y que nadie lo ha modificado en tránsito (Integridad)
            ciphertext = cipher.encrypt(nonce, msg_data.encode('utf-8'), None) 
            
            packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
            self.transport.sendto(packet, addr)
            return True
        except:
            return False

    def enviar_ack(self, ip, port, msg_id): 
        """
        Envía una confirmación de recepción (ACK) cifrada.
        Es necesario cifrar también los ACKs para evitar análisis de tráfico o inyección.
        """
        addr = (ip, port)
        if addr not in self.sessions:
            return
        try:
            cipher = self.sessions[addr]['cipher']
            nonce = os.urandom(12)
            # El contenido del ACK es simplemente el ID del mensaje confirmado
            ciphertext = cipher.encrypt(nonce, msg_id.encode('utf-8'), None)
            packet = struct.pack("B", PKT_ACK) + self.my_cid + nonce + ciphertext
            self.transport.sendto(packet, addr)
        except:
            pass

    def handle_ack(self, payload, addr): 
        """Procesa un ACK recibido, descifrándolo y notificando a la UI."""
        if addr not in self.sessions:
            return
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', 'Unknown')
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            msg_id = plaintext.decode('utf-8')
            if self.callback:
                self.callback(addr, f"ACK|{msg_id}", nombre, None)
        except:
            pass

    def enviar_reconnect_req(self, ip, port): 
        """Envía solicitud de reconexión (sin payload cifrado, solo cabecera)."""
        if not self.transport:
            return
        packet = struct.pack("B", PKT_RECONNECT_REQ) + self.my_cid
        self.transport.sendto(packet, (ip, port))

    def enviar_reconnect_resp(self, ip, port): 
        """Envía respuesta de reconexión aceptada."""
        if not self.transport:
            return
        packet = struct.pack("B", PKT_RECONNECT_RESP) + self.my_cid
        self.transport.sendto(packet, (ip, port))

    async def handle_reconnect_req(self, payload, addr):
        """
        Maneja solicitud de reconexión entrante.
        Verifica si existe una clave de sesión previa para esa IP/Puerto.
        Si existe, restaura la sesión y acepta la reconexión.
        """
        all_contacts = self.db.get_all_contacts()
        for cn, info in all_contacts.items():
            if info.get("ip") == addr[0] and info.get("port") == addr[1] and info.get("session_key"):
                try:
                    session_key = bytes.fromhex(info.get("session_key"))
                    # Restauración de sesión
                    self.sessions[addr] = {
                        'cipher': ChaCha20Poly1305(session_key),
                        'name': info.get("name", cn),
                        'state': 'ESTABLISHED'
                    }
                    self.db.set_contact_connected(cn, True)
                    self.role[addr] = "responder" # En reconexión, quien recibe el REQ actúa como Responder
                    self.enviar_reconnect_resp(addr[0], addr[1])
                    if self.callback:
                        self.callback(addr, "SESSION_RESTORED_RESP", info.get("name", cn), None)
                    return
                except Exception:
                    pass

    async def handle_reconnect_resp(self, payload, addr):
        """
        Maneja respuesta de reconexión.
        Confirma que la sesión ha sido restaurada exitosamente en el otro extremo.
        """
        if addr in self.reconnect_pending:
            info = self.reconnect_pending.pop(addr)
            cn = info['cn']
            if addr in self.sessions:
                self.role[addr] = "initiator" # Quien envió el REQ actúa como Iniciador
                session = self.sessions[addr]
                self.db.set_contact_connected(cn, True)
                if self.callback:
                    self.callback(addr, "SESSION_RESTORED_INIT", session.get("name", "Unknown"), None)

    def enviar_pending_send(self, ip, port):
        """Señalización: Notifica al peer que se iniciará la transmisión de mensajes pendientes."""
        if not self.transport:
            return
        try:
            packet = struct.pack("B", PKT_PENDING_SEND) + self.my_cid
            self.transport.sendto(packet, (ip, port))
        except Exception:
            pass

    def enviar_pending_done(self, ip, port):
        """Señalización: Notifica al peer que se ha finalizado la transmisión de pendientes."""
        if not self.transport:
            return
        try:
            packet = struct.pack("B", PKT_PENDING_DONE) + self.my_cid
            self.transport.sendto(packet, (ip, port))
        except Exception:
            pass

    async def handle_pending_send(self, payload, addr):
        """Procesa señal de inicio de envío de pendientes."""
        if addr not in self.sessions:
            return
        session = self.sessions[addr]
        nombre = session.get('name', 'Unknown')
        if self.callback:
            self.callback(addr, "PEER_SENDING_PENDING", nombre, None)

    def handle_pending_done(self, payload, addr):
        """
        Procesa señal de fin de envío de pendientes.
        Esto desencadena el turno del receptor para enviar sus propios mensajes pendientes (Half-Duplex lógico).
        """
        if addr not in self.sessions:
            return
        session = self.sessions[addr]
        nombre = session.get('name', 'Unknown')
        
        # Lógica de control de flujo: Si no he enviado mis pendientes aún, ahora es mi turno
        if not self.pending_sent.get(addr, False):
            self.pending_sent[addr] = True
            if self.callback:
                self.callback(addr, "SEND_MY_PENDING", nombre, None)

    async def check_reconnect_timeouts(self):
        """
        Tarea en segundo plano para limpiar intentos de reconexión fallidos.
        Evita que las conexiones queden en estado 'conectando' indefinidamente.
        """
        while True:
            await asyncio.sleep(1)
            
            current_time = asyncio.get_event_loop().time()
            timeout_addrs = []
            
            for addr, info in list(self.reconnect_pending.items()):
                if current_time - info['timestamp'] > 3: # Timeout de 3 segundos
                    timeout_addrs.append(addr)
            
            for addr in timeout_addrs:
                info = self.reconnect_pending.pop(addr)
                cn = info['cn']
                
                if addr in self.sessions:
                    del self.sessions[addr]
                
                if self.callback:
                    self.callback(addr, "RECONNECT_TIMEOUT", cn, None)
