# protocol.py
import asyncio
import struct
import os
import hashlib
# Importaciones de criptografía
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.backends import default_backend
import config

# Constantes de paquetes
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
        
        # --- NUEVO: Diccionario para guardar estado del handshake ---
        # Clave: "ip:port", Valor: "INITIATED", "RESPONSED", "ESTABLISHED"
        self.handshake_status = {} 

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 5: return
        msg_type = data[0]
        # El payload empieza en el byte 5 (después del tipo + CID)
        payload = data[5:]

        if msg_type == PKT_HANDSHAKE_INIT:
            asyncio.create_task(self.handle_handshake(payload, addr, is_response=False))
        elif msg_type == PKT_HANDSHAKE_RESP:
            asyncio.create_task(self.handle_handshake(payload, addr, is_response=True))
        elif msg_type == PKT_MSG:
            self.handle_message(payload, addr)

    async def handle_handshake(self, payload, addr, is_response):
        key = f"{addr[0]}:{addr[1]}"
        try:
            offset = 0
            # 1. Leer Clave Pública Efímera (32 bytes)
            if len(payload) < 32: raise ValueError("Paquete muy corto para pubkey")
            peer_pub_bytes = payload[offset : offset+32]
            offset += 32

            # 2. Leer Longitud del Certificado (2 bytes)
            if len(payload) < offset + 2: raise ValueError("Paquete muy corto para len cert")
            cert_len = struct.unpack("!H", payload[offset : offset+2])[0]
            offset += 2

            # 3. Leer Bytes del Certificado
            if len(payload) < offset + cert_len: raise ValueError("Paquete muy corto para cert")
            cert_bytes = payload[offset : offset+cert_len]
            offset += cert_len

            # 4. La firma está al final
            _signature = payload[offset:]

            # --- EXTRACCIÓN DEL NOMBRE ---
            try:
                cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
                # Usamos OID para buscar el Common Name de forma segura
                cn_attributes = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                if cn_attributes:
                    nombre = cn_attributes[0].value
                else:
                    nombre = "DNIe (Sin CN)"
            except Exception as e_cert:
                print(f"Error leyendo certificado: {e_cert}")
                nombre = "DNIe Inválido"

            # --- CRIPTOGRAFÍA ---
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            shared_secret = self.dnie.private_key.exchange(peer_key_obj)
            session_key = hashlib.blake2s(shared_secret, digest_size=32).digest()

            # Guardar sesión
            self.sessions[addr] = {
                'cipher': ChaCha20Poly1305(session_key),
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            
            # Actualizar estado del handshake
            self.handshake_status[key] = "ESTABLISHED"

            # Notificar a la GUI
            if self.callback:
                self.callback(addr, "HANDSHAKE_OK", nombre)

            # Si somos nosotros el servidor (quien recibió el INIT), respondemos
            if not is_response:
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
                # Marcamos como contestado
                self.handshake_status[key] = "RESPONSED"

        except Exception as e:
            print(f"❌ Error en handshake con {addr}: {e}")
            import traceback
            traceback.print_exc()
            # Limpiar estado si falla
            if key in self.handshake_status:
                del self.handshake_status[key]

    def handle_message(self, payload, addr):
        if addr not in self.sessions: return
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', '???')
        try:
            if len(payload) < 12: return
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            if self.callback:
                self.callback(addr, plaintext.decode('utf-8'), nombre)
        except Exception as e:
            print(f"Error descifrando msg de {nombre}: {e}")

    def enviar_handshake(self, ip, port):
        # Actualizamos el estado antes de enviar para que la GUI lo sepa
        key = f"{ip}:{port}"
        self.handshake_status[key] = "INITIATED"
        self._enviar_paquete_credenciales(ip, port, tipo=PKT_HANDSHAKE_INIT)

    def _enviar_paquete_credenciales(self, ip, port, tipo):
        if not self.transport: return
        try:
            cert, firma = self.dnie.obtener_credenciales()
            cert_len = len(cert)
            # Estructura: TIPO(1) + CID(4) + PUB(32) + LEN(2) + CERT + FIRMA
            packet = (
                struct.pack("B", tipo) + 
                self.my_cid + 
                self.dnie.public_bytes + 
                struct.pack("!H", cert_len) + 
                cert + 
                firma
            )
            self.transport.sendto(packet, (ip, port))
        except Exception as e:
            print(f"Error enviando credenciales: {e}")

    def enviar_mensaje(self, ip, port, texto):
        addr = (ip, port)
        if addr not in self.sessions:
            print(f"Intento de enviar mensaje a {addr} sin sesión.")
            return
        
        try:
            cipher = self.sessions[addr]['cipher']
            nonce = os.urandom(12)
            ciphertext = cipher.encrypt(nonce, texto.encode('utf-8'), None)
            packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
            self.transport.sendto(packet, addr)
        except Exception as e:
            print(f"Error enviando mensaje: {e}")