# protocol.py
import asyncio
import json
import base64
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend
from cryptography.fernet import Fernet, InvalidToken

class SecureIMProtocol(asyncio.DatagramProtocol):
    def __init__(self, dnie_manager, db, message_callback):
        self.dnie = dnie_manager
        self.db = db 
        self.message_callback = message_callback 
        self.transport = None

        self.session_keys = {} 
        self.peer_ephemeral_public_keys = {} 

        self.handshake_status = {} 
        self.my_ephemeral_private_keys = {} 

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        ip, port = addr
        
        try:
            message = json.loads(data.decode())
            msg_type = message.get("type")
            sender_cn_from_msg = message.get("sender_cn", f"{ip}:{port}") 

            real_cn = sender_cn_from_msg
            contact_info = self.db.get_contact_info(sender_cn_from_msg)
            if contact_info and contact_info.get("ip") == ip and contact_info.get("port") == port:
                real_cn = sender_cn_from_msg 

            if msg_type == "HANDSHAKE_INIT":
                asyncio.create_task(self._handle_handshake_init(message, addr))
            elif msg_type == "HANDSHAKE_RESPONSE":
                asyncio.create_task(self._handle_handshake_response(message, addr))
            elif msg_type == "ENCRYPTED_MESSAGE":
                asyncio.create_task(self._handle_encrypted_message(message, addr, real_cn))
            else:
                print(f"Mensaje desconocido de {addr}: {message}")

        except json.JSONDecodeError:
            print(f"Mensaje no JSON de {addr}: {data}")
        except Exception as e:
            print(f"Error procesando mensaje de {addr}: {e}")

    async def _handle_handshake_init(self, message, addr):
        ip, port = addr
        peer_nick_from_zeroconf = message["sender_nick"] 
        peer_ephemeral_public_bytes = base64.b64decode(message["public_key"])
        peer_dnie_cert_der = base64.b64decode(message["dnie_cert"])
        peer_dnie_signature = base64.b64decode(message["dnie_signature"])

        peer_cn_from_cert = self._get_cn_from_cert(x509.load_der_x509_certificate(peer_dnie_cert_der, default_backend()))
        
        self.db.add_or_update_contact(peer_cn_from_cert, ip=ip, port=port, update_seen=True)
        
        if self.db.get_contact_info(peer_cn_from_cert).get("is_connected"):
            print(f"Ya conectado a {peer_cn_from_cert}. Ignorando HANDSHAKE_INIT redundante.")
            return

        print(f"Recibido HANDSHAKE_INIT de {peer_cn_from_cert} ({addr})")
        self.handshake_status[peer_cn_from_cert] = "RESPONSED" 

        try:
            peer_cert = x509.load_der_x509_certificate(peer_dnie_cert_der, default_backend())
            peer_ephemeral_public_key = x25519.X25519PublicKey.from_public_bytes(peer_ephemeral_public_bytes)
            peer_cert.public_key().verify(
                peer_dnie_signature,
                peer_ephemeral_public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
                hashes.SHA256()
            )
            
            my_ephemeral_private_key = x25519.X25519PrivateKey.generate()
            # IMPORTANTE: No guardamos esta clave privada en self.my_ephemeral_private_keys
            # porque solo la necesitamos para este intercambio concreto.
            
            shared_key = my_ephemeral_private_key.exchange(peer_ephemeral_public_key)
            session_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b'handshake data',
                backend=default_backend()
            ).derive(shared_key)
            
            self.session_keys[peer_cn_from_cert] = Fernet(base64.urlsafe_b64encode(session_key))
            self.peer_ephemeral_public_keys[peer_cn_from_cert] = peer_ephemeral_public_key 

            my_dnie_cert, my_dnie_signature = self.dnie.obtener_credenciales()
            response_message = {
                "type": "HANDSHAKE_RESPONSE",
                "sender_cn": self.dnie.get_user_name(),
                "public_key": base64.b64encode(my_ephemeral_private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode(),
                "dnie_cert": base64.b64encode(my_dnie_cert).decode(),
                "dnie_signature": base64.b64encode(my_dnie_signature).decode()
            }
            self.transport.sendto(json.dumps(response_message).encode(), addr)

            self.db.set_contact_connected(peer_cn_from_cert, True)
            self.message_callback(addr, "HANDSHAKE_OK", peer_cn_from_cert)
            self.handshake_status[peer_cn_from_cert] = "COMPLETED"
            print(f"Handshake completado (RESPONSED) con {peer_cn_from_cert}")

        except Exception as e:
            print(f"Error en HANDSHAKE_INIT de {addr}: {e}")
            self.db.set_contact_connected(peer_cn_from_cert, False)
            self.message_callback(addr, "HANDSHAKE_ERROR", peer_cn_from_cert)
            self.handshake_status[peer_cn_from_cert] = "FAILED"

    async def _handle_handshake_response(self, message, addr):
        ip, port = addr
        peer_cn_from_msg = message["sender_cn"] 
        peer_ephemeral_public_bytes = base64.b64decode(message["public_key"])
        peer_dnie_cert_der = base64.b64decode(message["dnie_cert"])
        peer_dnie_signature = base64.b64decode(message["dnie_signature"])
        
        peer_cert = x509.load_der_x509_certificate(peer_dnie_cert_der, default_backend())
        peer_cn_from_cert = self._get_cn_from_cert(peer_cert)

        if peer_cn_from_msg != peer_cn_from_cert:
            print(f"ERROR: CN del mensaje '{peer_cn_from_msg}' no coincide con el CN del certificado '{peer_cn_from_cert}' de {addr}")
            self.db.set_contact_connected(peer_cn_from_cert, False) 
            self.message_callback(addr, "HANDSHAKE_ERROR", peer_cn_from_cert)
            self.handshake_status[peer_cn_from_cert] = "FAILED"
            return
        
        peer_cn = peer_cn_from_cert 

        if self.handshake_status.get(peer_cn) != "INITIATED":
            print(f"Recibido HANDSHAKE_RESPONSE inesperado de {peer_cn} ({addr}). Estado actual: {self.handshake_status.get(peer_cn)}")
            return 
        
        print(f"Recibido HANDSHAKE_RESPONSE de {peer_cn} ({addr})")

        try:
            peer_ephemeral_public_key = x25519.X25519PublicKey.from_public_bytes(peer_ephemeral_public_bytes)
            peer_cert.public_key().verify(
                peer_dnie_signature,
                peer_ephemeral_public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
                hashes.SHA256()
            )

            my_ephemeral_private_key = self.my_ephemeral_private_keys.get(peer_cn)
            if not my_ephemeral_private_key:
                raise RuntimeError(f"No se encontró clave efímera privada local para finalizar handshake con {peer_cn}.")

            shared_key = my_ephemeral_private_key.exchange(peer_ephemeral_public_key)
            session_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b'handshake data',
                backend=default_backend()
            ).derive(shared_key)
            
            self.session_keys[peer_cn] = Fernet(base64.urlsafe_b64encode(session_key))
            self.peer_ephemeral_public_keys[peer_cn] = peer_ephemeral_public_key

            self.db.set_contact_connected(peer_cn, True)
            self.message_callback(addr, "HANDSHAKE_OK", peer_cn)
            self.handshake_status[peer_cn] = "COMPLETED"
            del self.my_ephemeral_private_keys[peer_cn] 
            print(f"Handshake completado (INITIATED) con {peer_cn}. Clave de sesión establecida.")

        except Exception as e:
            print(f"Error en HANDSHAKE_RESPONSE de {addr}: {e}")
            self.db.set_contact_connected(peer_cn, False)
            self.message_callback(addr, "HANDSHAKE_ERROR", peer_cn)
            self.handshake_status[peer_cn] = "FAILED"
            if peer_cn in self.my_ephemeral_private_keys:
                del self.my_ephemeral_private_keys[peer_cn]


    async def _handle_encrypted_message(self, message, addr, sender_cn):
        encrypted_text = base64.b64decode(message["data"])
        
        if sender_cn not in self.session_keys:
            print(f"ERROR: Recibido mensaje cifrado de {sender_cn} sin clave de sesión. Estado Handshake: {self.handshake_status.get(sender_cn)}")
            # Si recibimos un mensaje cifrado sin sesión, intentamos un handshake de vuelta.
            self.enviar_handshake(addr[0], addr[1])
            self.message_callback(addr, "Sys: Error de descifrado. Reintentando conexión...", sender_cn)
            return

        try:
            decrypted_text = self.session_keys[sender_cn].decrypt(encrypted_text).decode()
            self.message_callback(addr, decrypted_text, sender_cn)
            self.db.set_contact_connected(sender_cn, True)

        except InvalidToken:
            print(f"ERROR: Token Fernet inválido de {sender_cn} (clave incorrecta o mensaje manipulado).")
            self.db.set_contact_connected(sender_cn, False) 
            self.message_callback(addr, "ERROR_CIFRADO", sender_cn)
            # No forzamos FAILED aquí para permitir reintentos si fue un paquete corrupto aislado
        except Exception as e:
            print(f"Error genérico al descifrar mensaje de {addr}: {e}")
            self.db.set_contact_connected(sender_cn, False)
            self.message_callback(addr, "ERROR_CIFRADO", sender_cn)

    def enviar_handshake(self, peer_ip, peer_port):
        peer_cn = None 
        for cn, info in self.db.get_all_contacts().items():
            if info.get("ip") == peer_ip and info.get("port") == peer_port:
                peer_cn = cn
                break
        
        if not peer_cn:
            print(f"No se puede iniciar handshake: CN desconocido para {peer_ip}:{peer_port}")
            return

        if self.db.get_contact_info(peer_cn).get("is_connected"):
            print(f"Ya conectado a {peer_cn}. No se inicia handshake.")
            return

        current_status = self.handshake_status.get(peer_cn)
        if current_status in ["INITIATED", "RESPONSED"]:
            print(f"Handshake con {peer_cn} ya en progreso (Estado: {current_status}). No se inicia uno nuevo.")
            return
            
        print(f"Iniciando handshake (INITIATED) con {peer_cn} ({peer_ip}:{peer_port})...")
        self.handshake_status[peer_cn] = "INITIATED" 

        try:
            my_ephemeral_private_key = x25519.X25519PrivateKey.generate()
            self.my_ephemeral_private_keys[peer_cn] = my_ephemeral_private_key
            
            my_dnie_cert, my_dnie_signature = self.dnie.obtener_credenciales()
            message = {
                "type": "HANDSHAKE_INIT",
                "sender_nick": self.dnie.get_user_name(), 
                "public_key": base64.b64encode(my_ephemeral_private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode(),
                "dnie_cert": base64.b64encode(my_dnie_cert).decode(),
                "dnie_signature": base64.b64encode(my_dnie_signature).decode()
            }
            self.transport.sendto(json.dumps(message).encode(), (peer_ip, peer_port))
            
            self.message_callback((peer_ip, peer_port), "HANDSHAKE_START", peer_cn)

        except Exception as e:
            print(f"Error al enviar handshake a {peer_ip}:{peer_port}: {e}")
            self.handshake_status[peer_cn] = "FAILED"
            self.message_callback((peer_ip, peer_port), "HANDSHAKE_ERROR", peer_cn)
            if peer_cn in self.my_ephemeral_private_keys:
                del self.my_ephemeral_private_keys[peer_cn]

    def enviar_mensaje(self, peer_ip, peer_port, text_message):
        peer_cn = None 
        for cn, info in self.db.get_all_contacts().items():
            if info.get("ip") == peer_ip and info.get("port") == peer_port:
                peer_cn = cn
                break

        if not peer_cn:
            print(f"No se puede enviar mensaje: CN desconocido para {peer_ip}:{peer_port}")
            return

        if not self.db.get_contact_info(peer_cn).get("is_connected") or peer_cn not in self.session_keys:
            print(f"INTENTO DE ENVÍO FALLIDO a {peer_cn}: Sin conexión segura o clave de sesión.")
            self.enviar_handshake(peer_ip, peer_port)
            return

        try:
            encrypted_data = self.session_keys[peer_cn].encrypt(text_message.encode())
            message = {
                "type": "ENCRYPTED_MESSAGE",
                "sender_cn": self.dnie.get_user_name(), 
                "data": base64.b64encode(encrypted_data).decode()
            }
            self.transport.sendto(json.dumps(message).encode(), (peer_ip, peer_port))
            print(f"Mensaje cifrado enviado a {peer_cn}")
        except Exception as e:
            print(f"Error al enviar mensaje cifrado a {peer_ip}:{peer_port}: {e}")

    def _get_cn_from_cert(self, cert):
        cn_attributes = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        if cn_attributes:
            raw_name = cn_attributes[0].value
            return raw_name.replace("(AUTENTICACIÓN)", "").replace("(FIRMA)", "").strip()
        return "CN_Desconocido"