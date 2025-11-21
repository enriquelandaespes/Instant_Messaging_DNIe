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
    def __init__(self, dnie_manager, on_msg_callback):
        self.dnie = dnie_manager
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
            # Aunque no la validemos todavía, es importante saber que está ahí
            signature = payload[offset:]

            # --- CORRECCIÓN PRINCIPAL ---
            # Cargar certificado
            cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
            
            # Extraer el Common Name (CN) de forma segura usando OIDs
            # Esto maneja correctamente los nombres con comas del DNIe (ej: "APELLIDO, NOMBRE")
            cn_attributes = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            if cn_attributes:
                nombre = cn_attributes[0].value
            else:
                nombre = "DNIe Desconocido"
            
            # --- FIN CORRECCIÓN ---

            # Crypto
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            shared_secret = self.dnie.private_key.exchange(peer_key_obj)
            session_key = hashlib.blake2s(shared_secret, digest_size=32).digest()
            
            self.sessions[addr] = {
                'cipher': ChaCha20Poly1305(session_key),
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            
            # Notificar a la GUI (callback wrapper en main.py)
            self.callback(addr, "HANDSHAKE_OK", nombre) 
            
            if not is_response:
                # Si nosotros recibimos la solicitud, debemos responder con nuestras credenciales
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)

        except Exception as e:
            print(f"❌ Error crítico en handshake con {addr}: {e}")
            import traceback
            traceback.print_exc() # Esto te ayudará a ver la línea exacta si falla otra cosa

    def handle_message(self, payload, addr):
        if addr not in self.sessions: return
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', '???')
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            self.callback(addr, plaintext.decode('utf-8'), nombre)
        except Exception as e: 
            print(f"Error descifrando mensaje: {e}")

    def enviar_handshake(self, ip, port):
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
        if addr not in self.sessions: return
        cipher = self.sessions[addr]['cipher']
        nonce = os.urandom(12)
        ciphertext = cipher.encrypt(nonce, texto.encode('utf-8'), None)
        packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
        self.transport.sendto(packet, addr)