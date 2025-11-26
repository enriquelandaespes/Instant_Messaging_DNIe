# protocol.py
import asyncio
import struct
import os
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography import x509
from cryptography.hazmat.backends import default_backend
import config

PKT_HANDSHAKE_INIT = 0x01  
PKT_MSG            = 0x02  
PKT_HANDSHAKE_RESP = 0x03  

class SecureIMProtocol(asyncio.DatagramProtocol):
    def __init__(self, dnie_manager, db, on_msg_callback):
        self.dnie = dnie_manager
        self.db = db  # <--- Referencia a la base de datos
        self.transport = None
        self.callback = on_msg_callback
        self.sessions = {} 
        self.my_cid = os.urandom(4)

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
            peer_pub_bytes = payload[offset : offset+32]
            offset += 32
            cert_len = struct.unpack("!H", payload[offset : offset+2])[0]
            offset += 2
            cert_bytes = payload[offset : offset+cert_len]
            offset += cert_len
            
            # Validación básica (TOFU)
            cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
            subject = cert_obj.subject.rfc4514_string()
            cn_part = [x for x in subject.split(',') if x.startswith('CN=')]
            # Limpiamos el nombre un poco más
            if cn_part:
                raw_name = cn_part[0].replace("CN=", "")
                nombre = raw_name.replace("(AUTENTICACIÓN)", "").replace("(Autenticación)", "").replace("(FIRMA)", "").strip()
            else:
                nombre = "Desconocido"
            
            # Crypto
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            shared_secret = self.dnie.private_key.exchange(peer_key_obj)
            session_key = hashlib.blake2s(shared_secret, digest_size=32).digest()
            
            # 1. Guardar en RAM
            self.sessions[addr] = {
                'cipher': ChaCha20Poly1305(session_key),
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            
            # 2. PERSISTENCIA: Guardar en DB para no repetir handshake si se reinicia
            contact_id = f"{addr[0]}:{addr[1]}"
            self.db.add_or_update_contact(
                contact_id, 
                name=nombre, 
                session_key=session_key.hex(),
                is_connected=True
            )
            
            if not is_response:
                self.callback(addr, "HANDSHAKE_OK", nombre) # Avisar UI
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
            else:
                self.callback(addr, "HANDSHAKE_OK", nombre) # Avisar UI

        except Exception as e:
            print(f"Error handshake: {e}")

    def handle_message(self, payload, addr):
        # Intentar restaurar sesión si no está en RAM
        if addr not in self.sessions:
            if not self.restaurar_sesion_si_existe(addr[0], addr[1]):
                return # No tenemos clave, ignorar mensaje
                
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', '???')
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            self.callback(addr, plaintext.decode('utf-8'), nombre)
        except: pass

    def enviar_handshake(self, ip, port):
        self._enviar_paquete_credenciales(ip, port, tipo=PKT_HANDSHAKE_INIT)

    def _enviar_paquete_credenciales(self, ip, port, tipo):
        cert, firma = self.dnie.obtener_credenciales()
        cert_len = len(cert)
        packet = (
            struct.pack("B", tipo) + self.my_cid + self.dnie.public_bytes + 
            struct.pack("!H", cert_len) + cert + firma
        )
        self.transport.sendto(packet, (ip, port))

    def enviar_mensaje(self, ip, port, texto):
        addr = (ip, port)
        # Intentar restaurar sesión si no está en RAM antes de enviar
        if addr not in self.sessions:
            if not self.restaurar_sesion_si_existe(ip, port):
                return False # Indica a la GUI que falló el envío por falta de sesión

        cipher = self.sessions[addr]['cipher']
        nonce = os.urandom(12)
        ciphertext = cipher.encrypt(nonce, texto.encode('utf-8'), None)
        packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
        self.transport.sendto(packet, addr)
        return True

    # --- NUEVAS FUNCIONES DE PERSISTENCIA ---
    def restaurar_sesion_si_existe(self, ip, port):
        """Intenta cargar la clave de sesión desde la DB a la RAM."""
        contact_id = f"{ip}:{port}"
        key_bytes = self.db.get_session_key(contact_id)
        
        if key_bytes:
            info = self.db.get_contact_info(contact_id)
            nombre = info.get("name", "Usuario") if info else "Usuario"
            
            self.sessions[(ip, port)] = {
                'cipher': ChaCha20Poly1305(key_bytes),
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            return True
        return False
    
    def tiene_sesion(self, ip, port):
        """Verifica si existe sesión en RAM o en Disco."""
        return (ip, port) in self.sessions or self.db.get_session_key(f"{ip}:{port}") is not None