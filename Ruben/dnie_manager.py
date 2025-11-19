# dnie_manager.py
import sys
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
        
        # 1. Generar claves efímeras
        self.private_key = x25519.X25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.public_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        
        # 2. Leer DNIe
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

    def obtener_credenciales(self):
        return self.cert_der, self.firma_cached

    def get_user_name(self):
        """Extrae el Common Name (CN) del certificado del DNIe."""
        try:
            cert = x509.load_der_x509_certificate(self.cert_der, default_backend())
            # El subject suele tener el formato: CN=APELLIDO1 APELLIDO2, NOMBRE (AUTENTICACIÓN)
            # O similar. Buscamos el atributo CommonName.
            cn_attributes = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            if cn_attributes:
                full_name = cn_attributes[0].value
                # Opcional: Limpiar un poco el nombre si es muy largo o tiene info extra
                # En DNIe suele ser "APELLIDOS NOMBRE (FIRMA)" o "(AUTENTICACIÓN)"
                return full_name
        except Exception as e:
            print(f"Error extrayendo nombre del certificado: {e}")
        
        return "Usuario_DNIe"