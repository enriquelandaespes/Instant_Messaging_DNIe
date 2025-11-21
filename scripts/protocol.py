# protocol.py
import asyncio
import struct
import os
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography import x509
from cryptography.x509.oid import NameOID  # <--- IMPORTANTE: Necesario para leer el nombre bien
from cryptography.hazmat.backends import default_backend
import config

PKT_HANDSHAKE_INIT = 0x01  
PKT_MSG            = 0x02  
PKT_HANDSHAKE_RESP = 0x03  

class SecureIMProtocol(asyncio.DatagramProtocol):
    def __init__(self, dnie_manager, db, on_msg_callback):
        self.dnie = dnie_manager
        self.db = db  # Añadido: Referencia a la base de datos
        self.transport = None
        self.callback = on_msg_callback
        self.sessions = {} 
        self.my_cid = os.urandom(4)
        self.handshake_in_progress = set()  # Direcciones con handshake en curso

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 5: return
        msg_type = data[0]
        payload = data[5:] 

        if msg_type == PKT_HANDSHAKE_INIT:
            asyncio.create_task(self.handle_handshake(payload, addr, is_response=False))
        elif msg_type == PKT_HANDSHAKE_RESP:
            asyncio.create_task(self.handle_handshake(payload, addr, is_response=True))
        elif msg_type == PKT_MSG:
            self.handle_message(payload, addr)

    async def handle_handshake(self, payload, addr, is_response):
        try:
            offset = 0
            # 1. Leer Clave Pública Efímera (32 bytes)
            peer_pub_bytes = payload[offset : offset+32]
            offset += 32
            
            # 2. Leer Longitud del Certificado (!H = unsigned short, 2 bytes)
            cert_len = struct.unpack("!H", payload[offset : offset+2])[0]
            offset += 2
            
            # 3. Leer Bytes del Certificado
            cert_bytes = payload[offset : offset+cert_len]
            offset += cert_len
            
            # 4. Leer la Firma (El resto del payload)
            signature = payload[offset:]

            # Cargar certificado y extraer CN
            cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
            cn_attributes = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            if cn_attributes:
                nombre_completo = cn_attributes[0].value
                # Limpiar sufijos del DNIe
                nombre = nombre_completo.replace("(AUTENTICACIÓN)", "").replace("(FIRMA)", "").strip()
            else:
                nombre = "DNIe Desconocido"
            
            # CORRECCIÓN: Buscar si ya existe un contacto con esta dirección
            existing_cn = self.db.find_contact_by_address(addr[0], addr[1])
            
            if existing_cn and existing_cn != nombre:
                # Hay un contacto descubierto con un nombre diferente al del certificado
                print(f"♻️ Consolidando contacto '{existing_cn}' → '{nombre}' ({addr[0]}:{addr[1]})")
                
                # Consolidar en la base de datos
                self.db.consolidate_contact(existing_cn, nombre)
                
                # Si había una sesión activa con el nombre antiguo, actualizarla
                old_addr = None
                for session_addr, session_data in list(self.sessions.items()):
                    if session_data.get('name') == existing_cn:
                        old_addr = session_addr
                        break
                
                # Notificar a la GUI sobre la consolidación
                await self.callback(addr, "CONTACT_CONSOLIDATED", nombre, old_cn=existing_cn)
            else:
                # Actualizar o crear el contacto normalmente
                self.db.add_or_update_contact(nombre, ip=addr[0], port=addr[1], update_seen=True)
            
            # Verificar si ya hay una sesión establecida
            if addr in self.sessions and self.sessions[addr].get('state') == 'ESTABLISHED':
                print(f"Ya conectado a {nombre} ({addr}). Ignorando handshake redundante.")
                return
            
            # Verificar si ya hay un handshake en progreso
            if addr in self.handshake_in_progress:
                print(f"Handshake con {nombre} ({addr}) ya en progreso. Ignorando duplicado.")
                return
            
            # Marcar handshake en progreso
            self.handshake_in_progress.add(addr)

            # Crypto
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            shared_secret = self.dnie.private_key.exchange(peer_key_obj)
            session_key = hashlib.blake2s(shared_secret, digest_size=32).digest()
            
            self.sessions[addr] = {
                'cipher': ChaCha20Poly1305(session_key),
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            
            # Actualizar estado de conexión en la DB
            self.db.set_contact_connected(nombre, True)
            
            # Notificar a la GUI
            await self.callback(addr, "HANDSHAKE_OK", nombre) 
            
            if not is_response:
                # Si nosotros recibimos la solicitud, debemos responder
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
            
            # Quitar de handshakes en progreso
            self.handshake_in_progress.discard(addr)

        except Exception as e:
            print(f"❌ Error crítico en handshake con {addr}: {e}")
            import traceback
            traceback.print_exc()
            # Quitar de handshakes en progreso en caso de error
            self.handshake_in_progress.discard(addr)

    def handle_message(self, payload, addr):
        if addr not in self.sessions: 
            # No hay sesión establecida, intentar handshake
            print(f"Mensaje recibido sin sesión de {addr}. Iniciando handshake...")
            self.enviar_handshake(addr[0], addr[1])
            return
            
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', '???')
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            
            # Actualizar estado de conexión al recibir mensaje exitosamente
            self.db.set_contact_connected(nombre, True)
            
            asyncio.create_task(self.callback(addr, plaintext.decode('utf-8'), nombre))
        except Exception as e: 
            print(f"Error descifrando mensaje de {nombre} ({addr}): {e}")
            # Invalidar sesión si falla el descifrado
            self.db.set_contact_connected(nombre, False)
            del self.sessions[addr]

    def enviar_handshake(self, ip, port):
        addr = (ip, port)
        
        # Verificar si ya hay handshake en progreso
        if addr in self.handshake_in_progress:
            print(f"Handshake con {ip}:{port} ya en progreso. No se inicia otro.")
            return
        
        # Verificar si ya hay sesión establecida
        if addr in self.sessions and self.sessions[addr].get('state') == 'ESTABLISHED':
            nombre = self.sessions[addr].get('name', 'desconocido')
            print(f"Ya conectado a {nombre} ({ip}:{port}). No se inicia handshake.")
            return
        
        print(f"Iniciando handshake con {ip}:{port}...")
        self.handshake_in_progress.add(addr)
        self._enviar_paquete_credenciales(ip, port, tipo=PKT_HANDSHAKE_INIT)

    def _enviar_paquete_credenciales(self, ip, port, tipo):
        cert, firma = self.dnie.obtener_credenciales()
        cert_len = len(cert)
        # Estructura: TIPO (1B) + CID (4B) + PUB_KEY (32B) + LEN_CERT (2B) + CERT (...) + FIRMA (...)
        packet = (
            struct.pack("B", tipo) + self.my_cid + self.dnie.public_bytes + 
            struct.pack("!H", cert_len) + cert + firma
        )
        self.transport.sendto(packet, (ip, port))

    def enviar_mensaje(self, ip, port, texto):
        addr = (ip, port)
        if addr not in self.sessions:
            print(f"No hay sesión con {ip}:{port}. Iniciando handshake primero...")
            self.enviar_handshake(ip, port)
            return False
        
        cipher = self.sessions[addr]['cipher']
        nonce = os.urandom(12)
        ciphertext = cipher.encrypt(nonce, texto.encode('utf-8'), None)
        packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
        self.transport.sendto(packet, addr)
        return True
