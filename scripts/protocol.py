# protocol.py
import asyncio
import struct
import os
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
import config

PKT_HANDSHAKE_INIT = 0x01  
PKT_MSG            = 0x02  
PKT_HANDSHAKE_RESP = 0x03  

class SecureIMProtocol(asyncio.DatagramProtocol):
    def __init__(self, dnie_manager, on_msg_callback):
        self.dnie = dnie_manager
        self.transport = None
        self.callback = on_msg_callback
        # CAMBIO CLAVE: Usamos addr (IP, Port) como clave principal de la sesión
        # Almacenamos el CID del remoto dentro de la sesión una vez conocido
        self.sessions = {}  
        self.my_cid = os.urandom(4) # Mi propio CID (identificador de conexión)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 5: return
        
        # HEADER: [Tipo (1B)] + [Sender CID (4B)] + [Payload...]
        msg_type = data[0]
        remote_cid = data[1:5] # Extraemos el CID del remitente del paquete
        payload = data[5:] 

        # Si es un Handshake (INIT o RESP), lo procesamos
        if msg_type == PKT_HANDSHAKE_INIT:
            asyncio.create_task(self.handle_handshake(payload, addr, remote_cid, is_response=False))
        elif msg_type == PKT_HANDSHAKE_RESP:
            asyncio.create_task(self.handle_handshake(payload, addr, remote_cid, is_response=True))
        elif msg_type == PKT_MSG:
            self.handle_message(payload, addr, remote_cid) # Pasar también el remote_cid

    async def handle_handshake(self, payload, addr, remote_cid, is_response):
        try:
            # print(f"DEBUG: Handshake recibido de {addr} (is_response={is_response})")
            offset = 0
            peer_pub_bytes = payload[offset : offset+32]
            offset += 32
            cert_len = struct.unpack("!H", payload[offset : offset+2])[0]
            offset += 2
            cert_bytes = payload[offset : offset+cert_len]
            # offset += cert_len # Esto no se usa para la firma en tu código, solo la pubkey del DNIe
            
            # Validación básica (TOFU) del certificado
            cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
            subject = cert_obj.subject.rfc4514_string()
            cn_part = [x for x in subject.split(',') if x.startswith('CN=')]
            nombre = cn_part[0].replace("CN=", "") if cn_part else "Desconocido"
            
            # Crypto: ECDH + HKDF (Requisito Guion)
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            shared_secret = self.dnie.private_key.exchange(peer_key_obj)
            
            # IMPLEMENTACIÓN DE HKDF CORRECTA
            hkdf = HKDF(
                algorithm=hashes.BLAKE2s(32),
                length=32,
                salt=None,
                info=b'dni-im-protocol',
                backend=default_backend()
            )
            session_key = hkdf.derive(shared_secret)
            
            # Guardar sesión usando la TUPLA (IP, Puerto) como clave
            # Almacenamos el CID del remoto dentro de la sesión
            self.sessions[addr] = {
                'cipher': ChaCha20Poly1305(session_key),
                'name': nombre,
                'remote_cid': remote_cid, # Guardamos el CID que nos ha enviado
                'state': 'ESTABLISHED'
            }
            
            if not is_response:
                self.callback(addr, "HANDSHAKE_OK", nombre) 
                # Si recibimos INIT, respondemos con RESP, incluyendo nuestro CID
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
            else: # is_response = True, hemos iniciado nosotros el handshake
                self.callback(addr, "HANDSHAKE_OK", nombre)

        except Exception as e:
            print(f"Error en handshake con {addr}: {e}")
            self.callback(addr, f"HANDSHAKE_ERROR", "Sys")

    def handle_message(self, payload, addr, remote_cid_from_packet):
        # Buscamos la sesión usando la tupla (IP, Puerto)
        if addr not in self.sessions: 
            # print(f"DEBUG: Mensaje de {addr} sin sesión establecida. Ignorando.")
            return
            
        session = self.sessions[addr]

        # Opcional: Podríamos verificar que remote_cid_from_packet coincide con session['remote_cid']
        # if remote_cid_from_packet != session['remote_cid']:
        #    print(f"DEBUG: CID mismatch para {addr}. Posible ataque o error. Ignorando.")
        #    return

        cipher = session['cipher']
        nombre = session.get('name', '???')
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            self.callback(addr, plaintext.decode('utf-8'), nombre)
        except Exception as e:
            print(f"Error al descifrar mensaje de {addr}: {e}")

    def enviar_handshake(self, ip, port):
        # Cuando iniciamos un handshake, aún no sabemos el CID del otro.
        # Simplemente enviamos nuestro CID.
        self._enviar_paquete_credenciales(ip, port, tipo=PKT_HANDSHAKE_INIT)

    def _enviar_paquete_credenciales(self, ip, port, tipo):
        cert, firma = self.dnie.obtener_credenciales()
        cert_len = len(cert)
        
        # El DNIe produce una clave pública de X25519 efímera al iniciar
        # Es la que se envía para el intercambio de claves
        packet = (
            struct.pack("B", tipo) + self.my_cid + self.dnie.public_bytes + 
            struct.pack("!H", cert_len) + cert + firma
        )
        self.transport.sendto(packet, (ip, port))

    def enviar_mensaje(self, ip, port, texto):
        target_addr = (ip, port)
        if target_addr not in self.sessions:
            print(f"ERROR: No hay sesión segura establecida con {target_addr}.")
            return

        session = self.sessions[target_addr]
        cipher = session['cipher']
        
        nonce = os.urandom(12)
        ciphertext = cipher.encrypt(nonce, texto.encode('utf-8'), None)
        
        # Enviamos MI CID en el encabezado del mensaje
        packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
        self.transport.sendto(packet, target_addr)