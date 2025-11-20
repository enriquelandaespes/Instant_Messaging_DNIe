# dnie_manager.py
import sys
import hashlib
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
        
        self.cert_der, self.firma_cached = self._extraer_credenciales()

    def _get_token(self):
        pkcs11 = pkcs11_lib(self.lib_path)
        slots = pkcs11.get_slots(token_present=True)
        if not slots:
            raise RuntimeError("No se detecta tarjeta DNIe.")
        return slots[config.SLOT_INDEX].get_token()

    def _extraer_credenciales(self):
        token = self._get_token()
        with token.open(user_pin=self.pin, rw=True) as session:
            certs = list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
            if not certs: raise RuntimeError("No certificados.")
            cert_der = certs[0][Attribute.VALUE] 

            keys = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
            if not keys: raise RuntimeError("No clave privada.")
            priv_key = keys[1] if len(keys) > 1 else keys[0]
            
            firma = priv_key.sign(self.public_bytes, mechanism=Mechanism.SHA256_RSA_PKCS)
            return cert_der, firma

    def sign_data(self, data: bytes) -> bytes:
        token = self._get_token()
        with token.open(user_pin=self.pin, rw=True) as session:
            keys = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
            priv_key = keys[1] if len(keys) > 1 else keys[0]
            return priv_key.sign(data, mechanism=Mechanism.SHA256_RSA_PKCS)

    def get_unique_id(self) -> str:
        cert = x509.load_der_x509_certificate(self.cert_der, default_backend())
        serial_number = str(cert.serial_number).encode('utf-8')
        signature = self.sign_data(serial_number)
        return hashlib.sha256(signature).hexdigest()[:16]

    def obtener_credenciales(self):
        return self.cert_der, self.firma_cached

    def get_user_name(self):
        """Extrae y LIMPIA el Common Name (CN) del certificado."""
        try:
            cert = x509.load_der_x509_certificate(self.cert_der, default_backend())
            cn_attributes = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            if cn_attributes:
                raw_name = cn_attributes[0].value
                # --- LIMPIEZA DE NOMBRE ---
                clean_name = raw_name.replace("(AUTENTICACIÓN)", "").replace("(FIRMA)", "").strip()
                # Quitamos comas extra si quedan al final o principio
                return clean_name
        except Exception:
            pass
        return "Usuario_DNIe"