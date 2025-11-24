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
PKT_ACK            = 0x04
PKT_RECONNECT      = 0x05  # Notificar reconexión con sesión guardada

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
        # Restaurar sesiones guardadas al inicio
        self.restaurar_sesiones_guardadas()

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
        elif msg_type == PKT_ACK:
            self.handle_ack(payload, addr)
        elif msg_type == PKT_RECONNECT:
            self.handle_reconnect(payload, addr)
        elif msg_type == PKT_RECONNECT:
            self.handle_reconnect(payload, addr)

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
            
            _ = payload[offset:]

            # --- LIMPIEZA DE NOMBRE ---
            try:
                cert_obj = x509.load_der_x509_certificate(cert_bytes, default_backend())
                cn_attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                if cn_attrs:
                    raw = cn_attrs[0].value
                    # Limpieza insensible a mayúsculas/minúsculas
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
            
            # Guardar la clave de sesión y certificado en la DB para uso futuro
            self.db.add_or_update_contact(
                nombre, 
                name=nombre,
                ip=addr[0], 
                port=addr[1],
                session_key=session_key.hex(),
                peer_cert=cert_bytes.hex()
            )
            
            if self.callback:
                self.callback(addr, "HANDSHAKE_OK", nombre)

            if not is_response:
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)

        except Exception as e:
            print(f"Handshake Error: {e}")

    def handle_message(self, payload, addr):
        if addr not in self.sessions: return
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', 'Unknown')
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            msg_data = plaintext.decode('utf-8')
            
            # Formato: msg_id|texto
            if '|' in msg_data:
                msg_id, msg = msg_data.split('|', 1)
            else:
                msg_id = None
                msg = msg_data
            
            if self.callback:
                # CAMBIO: Pasar msg_id junto con el mensaje
                # Formato nuevo del callback: (addr, mensaje_o_comando, nombre, msg_id)
                self.callback(addr, msg, nombre, msg_id)
            if not is_response:
                self._enviar_paquete_credenciales(addr[0], addr[1], tipo=PKT_HANDSHAKE_RESP)
    
        except Exception as e:
            print(f"Handshake Error: {e}")

    def enviar_handshake(self, ip, port, cn=None):
        """Envía handshake solo si no hay clave guardada"""
        addr = (ip, port)
        
        # Si ya tenemos sesión activa, no hacer nada
        if addr in self.sessions:
            return True
        
        # Si tenemos CN, verificar si hay clave guardada en DB
        if cn:
            saved_key = self.db.get_session_key(cn)
            if saved_key:
                # Restaurar sesión desde DB sin hacer handshake
                try:
                    self.sessions[addr] = {
                        'cipher': ChaCha20Poly1305(saved_key),
                        'name': cn,
                        'state': 'ESTABLISHED'
                    }
                    # Marcar como conectado
                    self.db.set_contact_connected(cn, True)
                    if self.callback:
                        self.callback(addr, "SESSION_RESTORED", cn, None)
                    return True
                except Exception as e:
                    print(f"Error crítico restaurando sesión: {e}")
                    # SEGURIDAD: Si falla la restauración, NO hacer handshake.
                    return False
        
        # No hay clave guardada - hacer handshake normal
        self._enviar_paquete_credenciales(ip, port, tipo=PKT_HANDSHAKE_INIT)
        return False
    
    def cerrar_sesion(self, ip, port):
        """Cierra una sesión existente"""
        addr = (ip, port)
        if addr in self.sessions:
            del self.sessions[addr]
    
    def tiene_sesion(self, ip, port):
        """Verifica si existe una sesión activa"""
        addr = (ip, port)
        return addr in self.sessions

    def _enviar_paquete_credenciales(self, ip, port, tipo):
        if not self.transport: return
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
        # Comprobación real de conexión
        if addr not in self.sessions:
            return False 
        
        try:
            cipher = self.sessions[addr]['cipher']
            nonce = os.urandom(12)
            # Formato: msg_id|texto
            msg_data = f"{msg_id}|{texto}" if msg_id else texto
            ciphertext = cipher.encrypt(nonce, msg_data.encode('utf-8'), None)
            packet = struct.pack("B", PKT_MSG) + self.my_cid + nonce + ciphertext
            self.transport.sendto(packet, addr)
            return True
        except:
            return False
    
    def enviar_ack(self, ip, port, msg_id):
        """Envía ACK de confirmación de entrega"""
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
        """Maneja ACK recibido y notifica a la GUI"""
        if addr not in self.sessions: return
        session = self.sessions[addr]
        cipher = session['cipher']
        nombre = session.get('name', 'Unknown')
        
        try:
            nonce = payload[:12]
            ciphertext = payload[12:]
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            msg_id = plaintext.decode('utf-8')
            
            # Notificar a la GUI que el mensaje fue entregado
            if self.callback:
                self.callback(addr, f"ACK|{msg_id}", nombre)
        except:
            pass
    
    def handle_reconnect(self, payload, addr):
        """Maneja notificación de reconexión del peer"""
        if addr not in self.sessions:
            return  # No tenemos sesión con este peer, ignorar
        
        session = self.sessions[addr]
        contact_name = session.get('name', 'Unknown')
        contact_id = f"{addr[0]}:{addr[1]}"
        
        print(f"🔄 {contact_name} se ha reconectado")
        
        # Notificar a la GUI que el contacto está online
        if self.callback:
            self.callback(addr, "PEER_RECONNECTED", contact_id)
    
    def enviar_reconnect(self, ip, port):
        """Envía notificación de reconexión a un peer con sesión guardada"""
        if not self.transport:
            return False
        
        addr = (ip, port)
        if addr not in self.sessions:
            return False
        
        try:
            # Paquete simple: solo tipo + CID
            packet = struct.pack("B", PKT_RECONNECT) + self.my_cid
            self.transport.sendto(packet, addr)
            return True
        except Exception as e:
            print(f"Error enviando RECONNECT: {e}")
            return False
    
    def restaurar_sesiones_guardadas(self):
        """Restaura todas las sesiones guardadas en la DB al iniciar"""
        try:
            contacts = self.db.get_all_contacts()
            for cn, info in contacts.items():
                session_key_hex = info.get("session_key")
                ip = info.get("ip")
            port = info.get("port")
            name = info.get("name", cn)
            
            if session_key_hex and ip and port:
                try:
                    session_key = bytes.fromhex(session_key_hex)
                    addr = (ip, port)
                    self.sessions[addr] = {
                        'cipher': ChaCha20Poly1305(session_key),
                        'name': name,
                        'state': 'ESTABLISHED'
                    }
                    print(f"✓ Sesión restaurada con {name} ({ip}:{port})")
                except Exception as e:
                    print(f"✗ Error restaurando sesión con {name}: {e}")
        except Exception as e:
            print(f"Error al restaurar sesiones: {e}")
