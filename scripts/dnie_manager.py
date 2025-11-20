# dnie_manager.py
import sys
import hashlib
import pkcs11.exceptions as pkcs11_exc # Importar excepciones para usar la correcta
from pkcs11 import lib as pkcs11_lib, ObjectClass, Attribute, Mechanism
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives import serialization
from cryptography import x509
from cryptography.hazmat.backends import default_backend
import config

class DNIeManager:
    def __init__(self, pin: str):
        self.pin = pin
        self.lib_path = config.PKCS11_LIB_PATH
        
        self.private_key = x25519.X25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.public_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        
        self.cert_der = None
        self.firma_cached = None
        
        # Intentamos extraer credenciales aquí.
        # Las excepciones (PinIncorrect, TokenNotPresent) serán manejadas en main.py
        self.cert_der, self.firma_cached = self._extraer_credenciales()


    def _get_token(self):
        pkcs11 = pkcs11_lib(self.lib_path)
        slots = pkcs11.get_slots(token_present=True)
        if not slots:
            # === CAMBIO AQUI: Lanzamos TokenNotPresent para que main.py lo capture ===
            raise pkcs11_exc.TokenNotPresent("No se detecta tarjeta DNIe.")
        return slots[config.SLOT_INDEX].get_token()

    def _extraer_credenciales(self):
        # Esta función podría lanzar PinIncorrect o TokenNotPresent
        # si la tarjeta no está o el PIN es incorrecto.
        token = self._get_token() # Esto podría lanzar TokenNotPresent
        with token.open(user_pin=self.pin, rw=True) as session: # Esto podría lanzar PinIncorrect
            certs = list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
            if not certs: raise RuntimeError("No certificados.")
            cert_der = certs[0][Attribute.VALUE] 

            keys = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
            if not keys: raise RuntimeError("No clave privada.")
            priv_key = keys[1] if len(keys) > 1 else keys[0]
            
            firma = priv_key.sign(self.public_bytes, mechanism=Mechanism.SHA256_RSA_PKCS)
            return cert_der, firma

    def sign_data(self, data: bytes) -> bytes:
        """Firma datos arbitrarios (usado para derivar clave de BD y ID único)."""
        token = self._get_token()
        with token.open(user_pin=self.pin, rw=True) as session:
            keys = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
            priv_key = keys[1] if len(keys) > 1 else keys[0]
            return priv_key.sign(data, mechanism=Mechanism.SHA256_RSA_PKCS)

    def get_unique_id(self) -> str:
        """
        Genera un ID único para el nombre de la BD.
        Extrae el número de serie del certificado y lo FIRMA con el DNIe.
        Luego hace un hash de esa firma.
        """
        # Necesitamos que cert_der ya esté cargado. Si no, significa que _extraer_credenciales falló
        if not self.cert_der:
            raise RuntimeError("Certificado DNIe no cargado para obtener ID único.")

        cert = x509.load_der_x509_certificate(self.cert_der, default_backend())
        serial_number = str(cert.serial_number).encode('utf-8')
        signature = self.sign_data(serial_number)
        return hashlib.sha256(signature).hexdigest()[:16]

    def obtener_credenciales(self):
        if not self.cert_der or not self.firma_cached:
            raise RuntimeError("Credenciales DNIe no cargadas.")
        return self.cert_der, self.firma_cached

    def get_user_name(self):
        """Extrae y LIMPIA el Common Name (CN) del certificado."""
        if not self.cert_der:
            return "Usuario_DNIe" # Fallback si el certificado no se cargó
        try:
            cert = x509.load_der_x509_certificate(self.cert_der, default_backend())
            cn_attributes = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            if cn_attributes:
                raw_name = cn_attributes[0].value
                clean_name = raw_name.replace("(AUTENTICACIÓN)", "").replace("(FIRMA)", "").strip()
                return clean_name
        except Exception:
            pass
        return "Usuario_DNIe"

    # La gestión de sesión ahora está principalmente dentro de 'with token.open()'
    # Si tuvieras una sesión global explícita, necesitarías un close_session.
    # Pero con el patrón 'with', se cierra automáticamente.
    def close_session(self):
        # Este método ya no es tan crítico con el uso de 'with token.open()'.
        # Se puede dejar como no-op o eliminar si no gestionas una sesión global.
        pass