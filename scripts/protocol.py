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
        self.db = db
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
            
            # Validación TOFU
            cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
            subject = cert_obj.subject.rfc4514_string()
            cn_part = [x for x in subject.split(',') if x.startswith('CN=')]
            
            if cn_part:
                raw = cn_part[0].replace("CN=", "")
                nombre = raw.replace("(AUTENTICACIÓN)", "").replace("(Autenticación)", "").replace("(FIRMA)", "").strip()
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
            
            # 2. Guardar en DB (Persistencia)
            contact_id = f"{addr[0]}:{addr[1]}"
            self.db.add_or_update_contact(
                contact_id, 
                name=nombre, 
                session_key=session_key.hex(),
                is_connected=True
            )
            
            self.callback(addr, "HANDSHAKE_OK", nombre)
            
            if not is_response:
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)

        except Exception as e:
            print(f"Error handshake: {e}")

    def handle_message(self, payload, addr):
        # Intentar restaurar sesión si existe en disco pero no en RAM
        if addr not in self.sessions:
            if not self.restaurar_sesion_si_existe(addr[0], addr[1]):
                return 

        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', '???')
        
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext_bytes = cipher.decrypt(nonce, ciphertext, None)
            full_text = plaintext_bytes.decode('utf-8')
            
            # LOGICA DE EXTRACCIÓN DE ID (Formato: "UUID::TEXTO")
            msg_id = None
            text = full_text
            if "::" in full_text:
                parts = full_text.split("::", 1)
                # Verificación básica de que parece un ID (longitud de UUID es 36)
                if len(parts[0]) == 36:
                    msg_id = parts[0]
                    text = parts[1]
            
            self.callback(addr, text, nombre, msg_id)
            
        except Exception:
            pass

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

    def enviar_mensaje(self, ip, port, texto, msg_id=None):
        addr = (ip, port)
        
        # Verificar sesión (Persistencia)
        if addr not in self.sessions:
            if not self.restaurar_sesion_si_existe(ip, port):
                return False 

        cipher = self.sessions[addr]['cipher']
        nonce = os.urandom(12)
        
        # EMPAQUETAR ID DENTRO DEL MENSAJE CIFRADO
        # Si hay ID, lo mandamos como "ID::TEXTO". Si no, solo "TEXTO"
        payload_str = f"{msg_id}::{texto}" if msg_id else texto
        
        ciphertext = cipher.encrypt(nonce, payload_str.encode('utf-8'), None)
        packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
        self.transport.sendto(packet, addr)
        return True

    def restaurar_sesion_si_existe(self, ip, port):
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
        return (ip, port) in self.sessions or self.db.get_session_key(f"{ip}:{port}") is not None