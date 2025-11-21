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
    def __init__(self, dnie_manager, db, on_msg_callback):
        self.dnie = dnie_manager
        self.db = db
        self.transport = None
        self.callback = on_msg_callback
        self.sessions = {}
        self.my_cid = os.urandom(4)
        
        # --- CORRECCIÓN: Renombrado a handshake_in_progress ---
        # Usamos un diccionario para guardar el estado:
        # Clave: tupla (ip, port). Valor: "INITIATED" | "ESTABLISHED"
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
        # addr es una tupla ('192.168.x.x', 6666)
        try:
            offset = 0
            # 1. Leer Clave Pública Efímera (32 bytes)
            if len(payload) < 32: raise ValueError("Paquete muy corto para pubkey")
            peer_pub_bytes = payload[offset : offset+32]
            offset += 32

            # 2. Leer Longitud del Certificado
            if len(payload) < offset + 2: raise ValueError("Paquete corto (len cert)")
            cert_len = struct.unpack("!H", payload[offset : offset+2])[0]
            offset += 2

            # 3. Leer Bytes del Certificado
            if len(payload) < offset + cert_len: raise ValueError("Paquete corto (cert)")
            cert_bytes = payload[offset : offset+cert_len]
            offset += cert_len

            # 4. Firma (el resto)
            _signature = payload[offset:]

            # --- EXTRACCIÓN DEL NOMBRE (DNIe) ---
            try:
                cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
                cn_attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                nombre = cn_attrs[0].value if cn_attrs else "DNIe Desconocido"
            except:
                nombre = "Error Certificado"

            # --- CRIPTOGRAFÍA ---
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            shared_secret = self.dnie.private_key.exchange(peer_key_obj)
            session_key = hashlib.blake2s(shared_secret, digest_size=32).digest()

            # Guardar sesión establecida
            self.sessions[addr] = {
                'cipher': ChaCha20Poly1305(session_key),
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            
            # Actualizar estado: Ya no está "in progress", está establecida.
            # Pero para que el check 'not in' de la GUI no falle si lo comprueba al revés,
            # lo mantenemos o lo marcamos como completado.
            # Si la GUI bloquea si ESTÁ en el dict, deberíamos borrarlo al acabar.
            # Si la GUI bloquea si NO ESTÁ conectado y NO ESTÁ en progreso, lo dejamos.
            self.handshake_in_progress[addr] = "ESTABLISHED"

            # Notificar GUI
            if self.callback:
                self.callback(addr, "HANDSHAKE_OK", nombre)

            # Responder si somos el servidor
            if not is_response:
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
                self.handshake_in_progress[addr] = "RESPONSED"

        except Exception as e:
            print(f"❌ Error handshake {addr}: {e}")
            # Si falla, liberamos el bloqueo para poder reintentar
            if addr in self.handshake_in_progress:
                del self.handshake_in_progress[addr]

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
            msg_str = plaintext.decode('utf-8')
            
            if self.callback:
                self.callback(addr, msg_str, nombre)
        except:
            pass

    def enviar_handshake(self, ip, port):
        # Usamos tupla como clave para ser consistentes con addr
        addr = (ip, port)
        self.handshake_in_progress[addr] = "INITIATED"
        self._enviar_paquete_credenciales(ip, port, tipo=PKT_HANDSHAKE_INIT)

    def _enviar_paquete_credenciales(self, ip, port, tipo):
        if not self.transport: return
        try:
            cert, firma = self.dnie.obtener_credenciales()
            cert_len = len(cert)
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
            # Si falla el envío, borramos el estado para permitir reintento
            addr = (ip, port)
            if addr in self.handshake_in_progress:
                del self.handshake_in_progress[addr]

    def enviar_mensaje(self, ip, port, texto):
        addr = (ip, port)
        if addr not in self.sessions:
            # Si no hay sesión, intentamos handshake automático o avisamos
            print(f"⚠️ No hay sesión segura con {addr}. Iniciando handshake...")
            self.enviar_handshake(ip, port)
            return
        
        try:
            cipher = self.sessions[addr]['cipher']
            nonce = os.urandom(12)
            ciphertext = cipher.encrypt(nonce, texto.encode('utf-8'), None)
            packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
            self.transport.sendto(packet, addr)
        except Exception as e:
            print(f"Error enviando mensaje: {e}")