import asyncio
import struct
import os
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric import x25519, padding
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.backends import default_backend
import config

# Definición de los diferentes tipos de paquetes que tenemos
PKT_EPHEMERAL_KEY = 0x01      # Fase 1 Handshake: Intercambio de clave pública efímera (X25519)
PKT_MSG = 0x02                # Mensaje de chat cifrado (Payload de aplicación)
PKT_ACK = 0x04                # Confirmación de recepción (Acknowledge)
PKT_RECONNECT_REQ = 0x05      # Solicitud de reconexión rápida (Session Resumption)
PKT_RECONNECT_RESP = 0x06     # Respuesta de reconexión aceptada
PKT_PENDING_SEND = 0x07       # Señalización: "Voy a empezar a enviar mensajes pendientes"
PKT_PENDING_DONE = 0x08       # Señalización: "He terminado de enviar mensajes pendientes"
PKT_HANDSHAKE_INIT = 0x10     # Fase 2 Handshake: Certificado cifrado del Iniciador
PKT_HANDSHAKE_RESP = 0x11     # Fase 2 Handshake: Certificado cifrado del Responder

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
        # Callback principal de recepción de paquetes UDP despacha el paquete según su tipo.
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
        """Fase 1: Recibe la clave pública efímera del peer"""
        try:
            if len(payload) < 32:
                return
            
            peer_ephemeral_pub_bytes = payload[:32]
            
            # Determinar si soy el iniciador o el responder
            is_initiator = addr in self.ephemeral_keys
            
            # Generar mi clave efímera si no existe (soy el responder)
            if not is_initiator:
                my_ephemeral_private = x25519.X25519PrivateKey.generate()
                self.ephemeral_keys[addr] = {
                    'private': my_ephemeral_private,
                    'public_bytes': my_ephemeral_private.public_key().public_bytes_raw()
                }
                # Enviar mi clave efímera de vuelta
                packet = struct.pack("B", PKT_EPHEMERAL_KEY) + self.my_cid + self.ephemeral_keys[addr]['public_bytes']
                self.transport.sendto(packet, addr)
            
            # Calcular secreto compartido efímero
            peer_ephemeral_key = x25519.X25519PublicKey.from_public_bytes(peer_ephemeral_pub_bytes)
            ephemeral_shared = self.ephemeral_keys[addr]['private'].exchange(peer_ephemeral_key)
            temp_key = hashlib.blake2s(ephemeral_shared, digest_size=32).digest()
            
            # Crear cifrador temporal para el certificado
            self.ephemeral_keys[addr]['temp_cipher'] = ChaCha20Poly1305(temp_key)
            self.ephemeral_keys[addr]['peer_public'] = peer_ephemeral_pub_bytes
            
            # Enviar certificado cifrado INMEDIATAMENTE
            if is_initiator:
                # Soy el iniciador: envío HANDSHAKE_INIT
                self.enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_INIT)
            else:
                # Soy el responder: envío HANDSHAKE_RESP
                self.enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
            
        except Exception:
            pass
    
    async def handle_handshake(self, payload, addr, is_response): 
        # Fase 2 del Handshake: 
        # 1. Recibe credenciales cifradas con claves efímeras.
        # 2. Valida la firma de la autoridad (Policía).
        # 3. Aplica política TOFU (Trust On First Use) para detectar suplantaciones.
        # 4. Establece la sesión permanente.
        
        # 1. Idempotencia: Si ya hay sesión establecida, ignoramos el paquete
        # NOTA: En renegociaciones reales podríamos querer actualizarla, pero mantenemos tu lógica
        if addr in self.sessions: 
            return
        
        # 2. Seguridad de flujo: Debe existir un contexto efímero (Fase 1 previa)
        if addr not in self.ephemeral_keys or 'temp_cipher' not in self.ephemeral_keys[addr]:
            return
        
        try:
            # Recuperamos el cifrador temporal negociado en la Fase 1
            temp_cipher = self.ephemeral_keys[addr]['temp_cipher']
            offset = 0
            
            # Validación de longitud mínima (32 key + 12 nonce + algo de cert)
            if len(payload) < 44: 
                return
            
            # EXTRACCIÓN DE DATOS 
            peer_pub_bytes = payload[offset:offset+32] # Clave pública permanente del DNIe (La identidad criptográfica)
            offset += 32
            
            nonce = payload[offset:offset+12] 
            offset += 12
            
            encrypted_cert = payload[offset:] 
            
            # DESCIFRADO Y AUTENTICACIÓN (AEAD) 
            try:
                # Si esto falla, el paquete fue modificado o no viene de quien tiene la clave efímera
                cert_bytes = temp_cipher.decrypt(nonce, encrypted_cert, None)
            except Exception:
                # print(f"Error de integridad en handshake desde {addr}")
                if addr in self.ephemeral_keys:
                    del self.ephemeral_keys[addr]
                return
            
            try:
                cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend()) # Carga del certificado
                cn_attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME) # Obtención de información del dni
                if cn_attrs:
                    raw = str(cn_attrs[0].value)
                    nombre = raw.replace("(AUTENTICACIÓN)", "").replace("(Autenticación)", "").replace("(FIRMA)", "").replace("(Firma)", "").strip() # Nos quedamos con el nombre limpio
                else:
                    nombre = "DNIe Desconocido"
            except:
                nombre = "Error Certificado"
                if addr in self.ephemeral_keys:
                    del self.ephemeral_keys[addr]
                return
            
            # CÁLCULO DEL SECRETO COMPARTIDO (ECDH PERMANENTE)
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes) # Obtención de la clave pública del otro usuario
            shared_secret = self.dnie.private_key.exchange(peer_key_obj) # Secreto compartido que solo conocen los dos usuarios
            session_key = hashlib.blake2s(shared_secret, digest_size=32).digest() # Generación de la clave de sesión
            
            self.sessions[addr] = { # Guardamos la sesión
                'cipher': ChaCha20Poly1305(session_key),
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            
            # ACTUALIZACIÓN DE BASE DE DATOS (Incluyendo public_key para TOFU)
            self.db.add_or_update_contact(
                nombre, # ID (nombre)
                name=nombre,
                ip=addr[0],
                port=addr[1],
                session_key=session_key.hex(),
                peer_cert=cert_bytes.hex(),
                public_key=peer_pub_bytes.hex() # Guardamos para futuro TOFU
            )
            
            if is_response: # Si somos los que iniciamos el handshake
                self.role[addr] = "initiator"
                cb_msg = "HANDSHAKE_OK_INIT"
            else:
                self.role[addr] = "responder" # Si respondemos al handshake
                cb_msg = "HANDSHAKE_OK_RESP"
            
            if self.callback:
                self.callback(addr, cb_msg, nombre, None)
            
            # Limpiar clave efímera después de todo
            if addr in self.ephemeral_keys:
                del self.ephemeral_keys[addr]
                
        except Exception:
            pass

    def handle_message(self, payload, addr): 
        # Manejamos el mensaje que nos llega 
        if addr not in self.sessions:
            return
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', 'Unknown')
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            msg_data = plaintext.decode('utf-8')
            
            if '|' in msg_data:
                msg_id, msg = msg_data.split('|', 1)
                self.enviar_ack(addr[0], addr[1], msg_id)
            else:
                msg_id = None
                msg = msg_data
            
            if self.callback:
                self.callback(addr, msg, nombre, msg_id)
            
            if msg_id:
                self.enviar_ack(addr[0], addr[1], msg_id)
        except:
            pass

    def enviar_handshake(self, ip, port, cn=None): 
        # Enviamos el handshake (Lógica original de reconexión rápida)
        addr = (ip, port)

        if addr in self.sessions:
            if self.callback:
                contact_name = self.sessions[addr].get('name', 'Unknown')
                self.callback(addr, "SESSIONS_OK", contact_name, None)
            return True
        
        saved_key = None
        contact_name = None
        
        # Busqueda de la clave guardada por nombre
        if cn:
            contact_info = self.db.get_contact_info(cn)
            if contact_info:
                saved_key = contact_info.get("session_key")
                contact_name = contact_info.get("name", cn)
        
        # Busqueda de la clave guardada en la base de datos por IP/Puerto
        if not saved_key:
            all_contacts = self.db.get_all_contacts()
            for name, info in all_contacts.items():
                if info.get("ip") == ip and info.get("port") == port:
                    saved_key = info.get("session_key")
                    if saved_key:
                        contact_name = info.get("name", name)
                        break
        
        # Intento de Reconexión Rápida (Protocolo original)
        if saved_key: 
            try:
                if isinstance(saved_key, str):
                    session_key = bytes.fromhex(saved_key)
                else:
                    session_key = saved_key

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
                
                self.enviar_reconnect_req(ip, port)
                return True
            except Exception:
                pass
        
        # Si falla reconexión -> Handshake Completo (Fase 1)
        self.enviar_clave_efimera(ip, port)
        return False
    
    def enviar_clave_efimera(self, ip, port):
        # Fase 1: Envía solo la clave pública efímera para establecer canal cifrado
        if not self.transport:
            return
        try:
            addr = (ip, port)
            # Generar nueva clave efímera para este handshake
            my_ephemeral_private = x25519.X25519PrivateKey.generate()
            public_bytes = my_ephemeral_private.public_key().public_bytes_raw()
            
            self.ephemeral_keys[addr] = {
                'private': my_ephemeral_private,
                'public_bytes': public_bytes
            }
            
            # Enviar solo la clave pública efímera
            packet = struct.pack("B", PKT_EPHEMERAL_KEY) + self.my_cid + public_bytes
            self.transport.sendto(packet, (ip, port))
        except Exception:
            pass

    def cerrar_sesion(self, ip, port): 
        # Cierre de sesión
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
        # Comprueba si existe una sesión con el contacto
        addr = (ip, port)
        return addr in self.sessions

    def enviar_paquete_credenciales(self, ip, port, tipo): 
        # Envio de paquete de credenciales al contacto (Fase 2 cifrada)
        if not self.transport:
            return
        addr = (ip, port)
        
        if addr not in self.ephemeral_keys or 'temp_cipher' not in self.ephemeral_keys[addr]:
            return
        
        try:
            cert, firma = self.dnie.obtener_credenciales()
            temp_cipher = self.ephemeral_keys[addr]['temp_cipher']
            
            # Cifrar el certificado con la clave temporal
            nonce = os.urandom(12) 
            encrypted_cert = temp_cipher.encrypt(nonce, cert, None)
            
            packet = (
                struct.pack("B", tipo) + self.my_cid + 
                self.dnie.public_bytes + nonce + encrypted_cert
            )
            self.transport.sendto(packet, (ip, port))
        except Exception:
            pass

    def enviar_mensaje(self, ip, port, texto, msg_id=None): 
        # Envio de mensaje al contacto
        addr = (ip, port)
        if addr not in self.sessions:
            return False
        try:
            cipher = self.sessions[addr]['cipher']
            nonce = os.urandom(12) 
            msg_data = f"{msg_id}|{texto}" if msg_id else texto
            ciphertext = cipher.encrypt(nonce, msg_data.encode('utf-8'), None) 
            
            packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
            self.transport.sendto(packet, addr)
            return True
        except:
            return False

    def enviar_ack(self, ip, port, msg_id): 
        # Envio de ACK al contacto
        addr = (ip, port)
        if addr not in self.sessions:
            return
        try:
            cipher = self.sessions[addr]['cipher']
            nonce = os.urandom(12)
            ciphertext = cipher.encrypt(nonce, msg_id.encode('utf-8'), None)
            packet = struct.pack("B", PKT_ACK) + self.my_cid + nonce + ciphertext
            self.transport.sendto(packet, addr)
        except:
            pass

    def handle_ack(self, payload, addr): 
        # Procesado del ACK
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
        # Envio de solicitud de reconexión al contacto
        if not self.transport:
            return
        packet = struct.pack("B", PKT_RECONNECT_REQ) + self.my_cid
        self.transport.sendto(packet, (ip, port))

    def enviar_reconnect_resp(self, ip, port): 
        # Envio de respuesta de reconexión al contacto
        if not self.transport:
            return
        packet = struct.pack("B", PKT_RECONNECT_RESP) + self.my_cid
        self.transport.sendto(packet, (ip, port))

    async def handle_reconnect_req(self, payload, addr):
        # Procesado de solicitud de reconexión
        all_contacts = self.db.get_all_contacts()
        for cn, info in all_contacts.items(): 
            if info.get("ip") == addr[0] and info.get("port") == addr[1] and info.get("session_key"): 
                try:
                    session_key = bytes.fromhex(info.get("session_key"))
                    self.sessions[addr] = { 
                        'cipher': ChaCha20Poly1305(session_key),
                        'name': info.get("name", cn),
                        'state': 'ESTABLISHED'
                    }
                    self.db.set_contact_connected(cn, True) 
                    self.role[addr] = "responder" 
                    self.enviar_reconnect_resp(addr[0], addr[1])
                    if self.callback: 
                        self.callback(addr, "SESSION_RESTORED_RESP", info.get("name", cn), None) 
                    return
                except Exception:
                    pass

    async def handle_reconnect_resp(self, payload, addr):
        # Procesado de respuesta de reconexión
        if addr in self.reconnect_pending:
            info = self.reconnect_pending.pop(addr)
            cn = info['cn']
            if addr in self.sessions:
                self.role[addr] = "initiator" 
                session = self.sessions[addr]
                self.db.set_contact_connected(cn, True)
                if self.callback: 
                    self.callback(addr, "SESSION_RESTORED_INIT", session.get("name", "Unknown"), None)

    def enviar_pending_send(self, ip, port):
        # Envio de paquete de paquetes pendientes
        if not self.transport:
            return
        try:
            packet = struct.pack("B", PKT_PENDING_SEND) + self.my_cid
            self.transport.sendto(packet, (ip, port))
        except Exception:
            pass

    def enviar_pending_done(self, ip, port):
        # Envio de paquete de paqutes pendienets ya enviados
        if not self.transport:
            return
        try:
            packet = struct.pack("B", PKT_PENDING_DONE) + self.my_cid
            self.transport.sendto(packet, (ip, port))
        except Exception:
            pass

    async def handle_pending_send(self, payload, addr):
        # Procesado de paquete de paquetes pendientes
        if addr not in self.sessions:
            return
        session = self.sessions[addr]
        nombre = session.get('name', 'Unknown')
        if self.callback:
            self.callback(addr, "PEER_SENDING_PENDING", nombre, None)

    def handle_pending_done(self, payload, addr):
        # Procesado de paquete de paquetes pendientes ya enviados
        if addr not in self.sessions:
            return
        session = self.sessions[addr]
        nombre = session.get('name', 'Unknown')
        
        if not self.pending_sent.get(addr, False):
            self.pending_sent[addr] = True
            if self.callback:
                self.callback(addr, "SEND_MY_PENDING", nombre, None)

    async def check_reconnect_timeouts(self):
        # Comprobación de timeouts de reconexión
        while True:
            await asyncio.sleep(1)
            current_time = asyncio.get_event_loop().time()
            timeout_addrs = []
            
            for addr, info in list(self.reconnect_pending.items()): # Recorremos los contactos pendientes de reconexión
                if current_time - info['timestamp'] > 0.5: # Timeout de 0.5 segundos
                    timeout_addrs.append(addr)
            
            for addr in timeout_addrs: # Si hay timeouts
                info = self.reconnect_pending.pop(addr) # Quitamos el contacto de la lista de pendientes
                cn = info['cn'] 
                if addr in self.sessions:
                    del self.sessions[addr] # Quitamos la sesión del contacto
                if self.callback:
                    self.callback(addr, "RECONNECT_TIMEOUT", cn, None) # Enviamos el callback