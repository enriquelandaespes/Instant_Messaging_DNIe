# protocol.py
import asyncio
import json
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend
from cryptography.fernet import Fernet, InvalidToken

class SecureIMProtocol(asyncio.DatagramProtocol):
    def __init__(self, dnie_manager, db, message_callback):
        self.dnie = dnie_manager
        self.db = db # Nueva dependencia de la base de datos
        self.message_callback = message_callback # Callback a la GUI para mensajes
        self.transport = None

        # Claves de sesión establecidas con cada peer {CN: FernetCipher}
        self.session_keys = {} 
        # Claves públicas efímeras recibidas {CN: X25519PublicKey}
        self.peer_ephemeral_public_keys = {}

        # Estado de los handshakes {CN: "PENDING", "COMPLETED", "FAILED"}
        self.handshake_state = {} 

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        ip, port = addr
        
        try:
            message = json.loads(data.decode())
            msg_type = message.get("type")
            sender_cn = message.get("sender_cn", f"{ip}:{port}") # CN o IP/Port temporal

            if msg_type == "HANDSHAKE_INIT":
                self._handle_handshake_init(message, addr)
            elif msg_type == "HANDSHAKE_RESPONSE":
                self._handle_handshake_response(message, addr)
            elif msg_type == "ENCRYPTED_MESSAGE":
                self._handle_encrypted_message(message, addr, sender_cn)
            else:
                print(f"Mensaje desconocido de {addr}: {message}")

        except json.JSONDecodeError:
            print(f"Mensaje no JSON de {addr}: {data}")
        except Exception as e:
            print(f"Error procesando mensaje de {addr}: {e}")

    async def _handle_handshake_init(self, message, addr):
        ip, port = addr
        peer_nick = message["sender_nick"] # Nick de Zeroconf
        peer_ephemeral_public_bytes = base64.b64decode(message["public_key"])
        peer_dnie_cert_der = base64.b64decode(message["dnie_cert"])
        peer_dnie_signature = base64.b64decode(message["dnie_signature"])

        try:
            # 1. Verificar certificado DNIe del peer
            peer_cert = x509.load_der_x509_certificate(peer_dnie_cert_der, default_backend())
            peer_cn_from_cert = self._get_cn_from_cert(peer_cert)

            # 2. Verificar firma DNIe del peer sobre su clave efímera
            peer_ephemeral_public_key = x25519.X25519PublicKey.from_public_bytes(peer_ephemeral_public_bytes)
            peer_cert.public_key().verify(
                peer_dnie_signature,
                peer_ephemeral_public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
                hashes.SHA256()
            )
            
            # 3. Derivar clave de sesión
            shared_key = self.dnie.private_key.exchange(peer_ephemeral_public_key)
            session_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b'handshake data',
                backend=default_backend()
            ).derive(shared_key)
            
            self.session_keys[peer_cn_from_cert] = Fernet(base64.urlsafe_b64encode(session_key))
            self.peer_ephemeral_public_keys[peer_cn_from_cert] = peer_ephemeral_public_key

            # 4. Enviar respuesta de Handshake
            my_dnie_cert, my_dnie_signature = self.dnie.obtener_credenciales()
            response_message = {
                "type": "HANDSHAKE_RESPONSE",
                "sender_cn": self.dnie.get_user_name(),
                "public_key": base64.b64encode(self.dnie.public_bytes).decode(),
                "dnie_cert": base64.b64encode(my_dnie_cert).decode(),
                "dnie_signature": base64.b64encode(my_dnie_signature).decode()
            }
            self.transport.sendto(json.dumps(response_message).encode(), addr)

            # 5. Marcar como conectado y notificar a la GUI
            self.db.set_contact_connected(peer_cn_from_cert, True)
            self.message_callback(addr, "HANDSHAKE_OK", peer_cn_from_cert)
            self.handshake_state[peer_cn_from_cert] = "COMPLETED"

        except Exception as e:
            print(f"Error en HANDSHAKE_INIT de {addr}: {e}")
            self.db.set_contact_connected(peer_cn_from_cert, False)
            self.message_callback(addr, "HANDSHAKE_ERROR", peer_cn_from_cert)
            self.handshake_state[peer_cn_from_cert] = "FAILED"

    async def _handle_handshake_response(self, message, addr):
        ip, port = addr
        peer_cn = message["sender_cn"] # CN del respondedor
        peer_ephemeral_public_bytes = base64.b64decode(message["public_key"])
        peer_dnie_cert_der = base64.b64decode(message["dnie_cert"])
        peer_dnie_signature = base64.b64decode(message["dnie_signature"])

        try:
            # 1. Verificar certificado DNIe del peer
            peer_cert = x509.load_der_x509_certificate(peer_dnie_cert_der, default_backend())
            peer_cn_from_cert = self._get_cn_from_cert(peer_cert)

            # 2. Verificar que el CN del mensaje coincide con el del certificado
            if peer_cn != peer_cn_from_cert:
                raise ValueError("CN del mensaje no coincide con el CN del certificado.")

            # 3. Verificar firma DNIe del peer sobre su clave efímera
            peer_ephemeral_public_key = x25519.X25519PublicKey.from_public_bytes(peer_ephemeral_public_bytes)
            peer_cert.public_key().verify(
                peer_dnie_signature,
                peer_ephemeral_public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
                hashes.SHA256()
            )

            # 4. Derivar clave de sesión (usamos la misma lógica que en INIT)
            shared_key = self.dnie.private_key.exchange(peer_ephemeral_public_key)
            session_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b'handshake data',
                backend=default_backend()
            ).derive(shared_key)
            
            self.session_keys[peer_cn_from_cert] = Fernet(base64.urlsafe_b64encode(session_key))
            self.peer_ephemeral_public_keys[peer_cn_from_cert] = peer_ephemeral_public_key

            # 5. Marcar como conectado y notificar a la GUI
            self.db.set_contact_connected(peer_cn_from_cert, True)
            self.message_callback(addr, "HANDSHAKE_OK", peer_cn_from_cert)
            self.handshake_state[peer_cn_from_cert] = "COMPLETED"

        except Exception as e:
            print(f"Error en HANDSHAKE_RESPONSE de {addr}: {e}")
            self.db.set_contact_connected(peer_cn_from_cert, False)
            self.message_callback(addr, "HANDSHAKE_ERROR", peer_cn_from_cert)
            self.handshake_state[peer_cn_from_cert] = "FAILED"
            
    async def _handle_encrypted_message(self, message, addr, sender_cn):
        encrypted_text = base64.b64decode(message["data"])
        
        try:
            if sender_cn not in self.session_keys:
                print(f"No hay clave de sesión para {sender_cn}. Handshake pendiente?")
                # Intentar iniciar handshake si no hay clave de sesión
                contact_info = self.db.get_contact_info(sender_cn)
                if contact_info and not contact_info.get("is_connected"):
                    # Solo enviamos handshake si no estamos ya "conectados"
                    self.enviar_handshake(addr[0], addr[1])
                return

            decrypted_text = self.session_keys[sender_cn].decrypt(encrypted_text).decode()
            self.message_callback(addr, decrypted_text, sender_cn)

        except InvalidToken:
            print(f"Mensaje cifrado no válido de {addr} (clave incorrecta o mensaje manipulado).")
            # Podríamos desconectar o pedir un re-handshake
            self.db.set_contact_connected(sender_cn, False)
            self.message_callback(addr, "ERROR_CIFRADO", sender_cn)
        except Exception as e:
            print(f"Error al descifrar mensaje de {addr}: {e}")
            self.db.set_contact_connected(sender_cn, False)
            self.message_callback(addr, "ERROR_CIFRADO", sender_cn)

    def enviar_handshake(self, peer_ip, peer_port):
        peer_cn = None # Necesitamos una forma de identificar al peer por IP/Port si no tenemos CN
        
        # Buscar el CN en la DB por IP/Puerto para ver si ya tenemos un estado
        for cn, info in self.db.get_all_contacts().items():
            if info.get("ip") == peer_ip and info.get("port") == peer_port:
                peer_cn = cn
                break

        if peer_cn and self.db.get_contact_info(peer_cn).get("is_connected"):
            print(f"Ya conectado a {peer_cn}. No se inicia handshake.")
            return

        print(f"Iniciando handshake con {peer_ip}:{peer_port}...")
        try:
            my_dnie_cert, my_dnie_signature = self.dnie.obtener_credenciales()
            message = {
                "type": "HANDSHAKE_INIT",
                "sender_nick": self.dnie.get_user_name(), # Usamos el nick de Zeroconf/Display
                "public_key": base64.b64encode(self.dnie.public_bytes).decode(),
                "dnie_cert": base64.b64encode(my_dnie_cert).decode(),
                "dnie_signature": base64.b64encode(my_dnie_signature).decode()
            }
            self.transport.sendto(json.dumps(message).encode(), (peer_ip, peer_port))
            
            if peer_cn:
                self.handshake_state[peer_cn] = "PENDING"
        except Exception as e:
            print(f"Error al enviar handshake a {peer_ip}:{peer_port}: {e}")

    def enviar_mensaje(self, peer_ip, peer_port, text_message):
        peer_cn = None 
        # Buscar el CN en la DB por IP/Puerto
        for cn, info in self.db.get_all_contacts().items():
            if info.get("ip") == peer_ip and info.get("port") == peer_port:
                peer_cn = cn
                break

        if not peer_cn or peer_cn not in self.session_keys:
            print(f"No se puede enviar mensaje a {peer_ip}:{peer_port}. Sin clave de sesión.")
            # Si no hay clave de sesión, intentar un handshake
            self.enviar_handshake(peer_ip, peer_port)
            return

        try:
            encrypted_data = self.session_keys[peer_cn].encrypt(text_message.encode())
            message = {
                "type": "ENCRYPTED_MESSAGE",
                "sender_cn": self.dnie.get_user_name(), # Nuestro CN para que el receptor sepa quién somos
                "data": base64.b64encode(encrypted_data).decode()
            }
            self.transport.sendto(json.dumps(message).encode(), (peer_ip, peer_port))
        except Exception as e:
            print(f"Error al enviar mensaje cifrado a {peer_ip}:{peer_port}: {e}")

    def _get_cn_from_cert(self, cert):
        cn_attributes = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        if cn_attributes:
            raw_name = cn_attributes[0].value
            return raw_name.replace("(AUTENTICACIÓN)", "").replace("(FIRMA)", "").strip()
        return "CN_Desconocido"