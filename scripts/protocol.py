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
PKT_MSG = 0x02
PKT_HANDSHAKE_RESP = 0x03
PKT_ACK = 0x04
PKT_RECONNECT = 0x05

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
        if self.callback:
            self.callback(None, "SESSIONS_READY", "System", None)

    def datagram_received(self, data, addr):
        if len(data) < 5:
            return
        msg_type = data[0]
        payload = data[5:]
        
        if msg_type == PKT_HANDSHAKE_INIT:
            asyncio.create_task(self.handle_handshake(payload, addr, is_response=False))
        elif msg_type == PKT_HANDSHAKE_RESP:
            asyncio.create_task(self.handle_handshake(payload, addr, is_response=True))
        elif msg_type == PKT_MSG:
            self.handle_message(payload, addr)
        elif msg_type == PKT_ACK:
            self.handle_ack(payload, addr)
        elif msg_type == PKT_RECONNECT:
            self.handle_reconnect(payload, addr)

    async def handle_handshake(self, payload, addr, is_response):
        # Si ya tenemos sesión, ignorar
        if addr in self.sessions:
            return
        
        try:
            offset = 0
            if len(payload) < 32:
                return
            
            peer_pub_bytes = payload[offset:offset+32]
            offset += 32
            
            cert_len = struct.unpack("!H", payload[offset:offset+2])[0]
            offset += 2
            
            cert_bytes = payload[offset:offset+cert_len]
            offset += cert_len
            
            try:
                cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
                cn_attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                if cn_attrs:
                    raw = cn_attrs[0].value
                    nombre = raw.replace("(AUTENTICACIÓN)", "").replace("(Autenticación)", "").replace("(FIRMA)", "").replace("(Firma)", "").strip()
                else:
                    nombre = "DNIe Desconocido"
            except:
                nombre = "Error Certificado"
            
            peer_key_obj = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
            shared_secret = self.dnie.private_key.exchange(peer_key_obj)
            session_key = hashlib.blake2s(shared_secret, digest_size=32).digest()
            
            self.sessions[addr] = {
                'cipher': ChaCha20Poly1305(session_key),
                'name': nombre,
                'state': 'ESTABLISHED'
            }
            
            # Buscar si ya existe contacto con esta IP/puerto
            existing_cn = None
            all_contacts = self.db.get_all_contacts()
            for cn, info in all_contacts.items():
                if info.get("ip") == addr[0] and info.get("port") == addr[1]:
                    existing_cn = cn
                    break
            
            # Guardar en BD
            contact_id = existing_cn if existing_cn else nombre
            self.db.add_or_update_contact(
                contact_id,
                name=nombre,
                ip=addr[0],
                port=addr[1],
                session_key=session_key.hex(),
                peer_cert=cert_bytes.hex()
            )
            
            if self.callback:
                self.callback(addr, "HANDSHAKE_OK", nombre, None)
            
            # Si recibimos INIT, responder con RESP
            if not is_response:
                self.enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
                
        except Exception:
            pass

    def handle_message(self, payload, addr):
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
        """
        Decide si enviar PKT_RECONNECT (si hay session_key guardada) o PKT_HANDSHAKE_INIT (primera vez).
        """
        addr = (ip, port)

        # Si ya hay sesión en memoria, no hacer nada
        if addr in self.sessions:
            return True
        
        # Buscar session_key guardada
        saved_key = None
        contact_name = None
        
        # 1. Buscar por CN
        if cn:
            contact_info = self.db.get_contact_info(cn)
            if contact_info:
                saved_key = contact_info.get("session_key")
                contact_name = contact_info.get("name", cn)
        
        # 2. Buscar por IP/puerto
        if not saved_key:
            all_contacts = self.db.get_all_contacts()
            for name, info in all_contacts.items():
                if info.get("ip") == ip and info.get("port") == port:
                    saved_key = info.get("session_key")
                    if saved_key:
                        contact_name = info.get("name", name)
                        break
        
        # Si hay clave guardada: restaurar sesión y enviar PKT_RECONNECT
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
                self.db.set_contact_connected(final_cn, True)
                
                if self.callback:
                    self.callback(addr, "SESSION_RESTORED", contact_name, None)
                
                # Enviar PKT_RECONNECT (sin credenciales)
                self.enviar_reconnect(ip, port)
                return True
            except Exception:
                pass
        
        # No hay clave: handshake inicial completo
        self.enviar_paquete_credenciales(ip, port, tipo=PKT_HANDSHAKE_INIT)
        return False

    def cerrar_sesion(self, ip, port):
        addr = (ip, port)
        if addr in self.sessions:
            del self.sessions[addr]

    def tiene_sesion(self, ip, port):
        addr = (ip, port)
        return addr in self.sessions

    def enviar_paquete_credenciales(self, ip, port, tipo):
        if not self.transport:
            return
        try:
            cert, firma = self.dnie.obtener_credenciales()
            packet = (
                struct.pack("B", tipo) + self.my_cid + self.dnie.public_bytes +
                struct.pack("!H", len(cert)) + cert + firma
            )
            self.transport.sendto(packet, (ip, port))
        except Exception:
            pass

    def enviar_mensaje(self, ip, port, texto, msg_id=None):
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

    def handle_reconnect(self, payload, addr):
        """
        Recibe PKT_RECONNECT: restaura sesión desde BD y responde con PKT_RECONNECT.
        """
        if addr not in self.sessions:
            # Buscar contacto en BD por IP/puerto
            all_contacts = self.db.get_all_contacts()
            for cn, info in all_contacts.items():
                if info.get('ip') == addr[0] and info.get('port') == addr[1]:
                    session_key_hex = info.get("session_key")
                    if not session_key_hex:
                        continue
                    try:
                        session_key = bytes.fromhex(session_key_hex)
                        self.sessions[addr] = {
                            'cipher': ChaCha20Poly1305(session_key),
                            'name': info.get("name", cn),
                            'state': 'ESTABLISHED'
                        }
                        self.db.set_contact_connected(cn, True)
                        
                        # Responder con PKT_RECONNECT
                        self.enviar_reconnect(addr[0], addr[1])
                        
                        if self.callback:
                            self.callback(addr, "SESSION_RESTORED", info.get("name", cn), None)
                        return
                    except Exception:
                        pass
            return
        
        # Ya tenemos sesión: solo notificar
        session = self.sessions[addr]
        contact_name = session.get('name', 'Unknown')
        if self.callback:
            self.callback(addr, "PEER_RECONNECTED", contact_name, None)

    def enviar_reconnect(self, ip, port):
        if not self.transport:
            return False
        addr = (ip, port)
        if addr not in self.sessions:
            return False
        try:
            packet = struct.pack("B", PKT_RECONNECT) + self.my_cid
            self.transport.sendto(packet, addr)
            return True
        except Exception:
            return False
