# protocol.py
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
        self.handshake_in_progress = {} 

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
            if len(payload) < 32: return
            peer_pub_bytes = payload[offset : offset+32]
            offset += 32
            
            cert_len = struct.unpack("!H", payload[offset : offset+2])[0]
            offset += 2
            
            cert_bytes = payload[offset : offset+cert_len]
            offset += cert_len
            
            # (Ignoramos firma pero leemos offset)
            _ = payload[offset:]

            # Nombre DNIe limpio
            try:
                cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
                cn_attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                if cn_attrs:
                    nombre = cn_attrs[0].value.replace("(Autenticación)", "").replace("(Firma)", "").strip()
                else:
                    nombre = "DNIe Desconocido"
            except:
                nombre = "Error Certificado"

            # Crypto
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            shared_secret = self.dnie.private_key.exchange(peer_key_obj)
            session_key = hashlib.blake2s(shared_secret, digest_size=32).digest()
            
            self.sessions[addr] = {
                'cipher': ChaCha20Poly1305(session_key),
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            self.handshake_in_progress[addr] = "ESTABLISHED"
            
            if self.callback:
                self.callback(addr, "HANDSHAKE_OK", nombre)

            if not is_response:
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
                self.handshake_in_progress[addr] = "RESPONSED"

        except Exception as e:
            print(f"Handshake Error: {e}")
            if addr in self.handshake_in_progress:
                del self.handshake_in_progress[addr]

    def handle_message(self, payload, addr):
        if addr not in self.sessions: return
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', 'Unknown')
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            msg = plaintext.decode('utf-8')
            
            if self.callback:
                self.callback(addr, msg, nombre)
        except: pass

    def enviar_handshake(self, ip, port):
        addr = (ip, port)
        self.handshake_in_progress[addr] = "INITIATED"
        self._enviar_paquete_credenciales(ip, port, tipo=PKT_HANDSHAKE_INIT)

    def _enviar_paquete_credenciales(self, ip, port, tipo):
        if not self.transport: return
        try:
            cert, firma = self.dnie.obtener_credenciales()
            packet = (
                struct.pack("B", tipo) + self.my_cid + self.dnie.public_bytes + 
                struct.pack("!H", len(cert)) + cert + firma
            )
            self.transport.sendto(packet, (ip, port))
        except Exception as e:
            print(f"Error env credenciales: {e}")
            if (ip, port) in self.handshake_in_progress:
                del self.handshake_in_progress[(ip, port)]

    def enviar_mensaje(self, ip, port, texto):
        """ Devuelve True si se envió, False si no hay conexión """
        addr = (ip, port)
        
        # 1. COMPROBACIÓN DE CONEXIÓN
        if addr not in self.sessions:
            # No hay sesión criptográfica establecida
            return False 
        
        try:
            cipher = self.sessions[addr]['cipher']
            nonce = os.urandom(12)
            ciphertext = cipher.encrypt(nonce, texto.encode('utf-8'), None)
            packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
            self.transport.sendto(packet, addr)
            return True # Enviado correctamente
        except Exception as e:
            print(f"Error socket msg: {e}")
            return False # Fallo en el envío