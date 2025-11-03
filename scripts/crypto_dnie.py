"""Interfaz con DNIe mediante PKCS#11"""
from pkcs11 import lib as pkcs11_lib, ObjectClass, Attribute
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class DNIeInterface:
    def __init__(self, pkcs11_lib_path: str = "/usr/lib/libpkcs11-dnie.so"):
        self.pkcs11 = pkcs11_lib.PyKCS11Lib()
        self.pkcs11.load(pkcs11_lib_path)
        self.session = None
        self.certificate = None
        self.private_key = None
        
    def connect(self, pin: str) -> bool:
        try:
            slots = self.pkcs11.getSlotList(tokenPresent=True)
            if not slots:
                return False
            
            slot = slots[0]
            self.session = self.pkcs11.openSession(slot)
            self.session.login(pin)
            
            certs = self.session.findObjects([
                (ObjectClass.CKA_CLASS, ObjectClass.CKO_CERTIFICATE)
            ])
            if certs:
                cert_der = bytes(self.session.getAttributeValue(
                    certs[0], [ObjectClass.CKA_VALUE]
                )[0])
                self.certificate = x509.load_der_x509_certificate(cert_der)
                
            priv_keys = self.session.findObjects([
                (ObjectClass.CKA_CLASS, ObjectClass.CKO_PRIVATE_KEY),
                (ObjectClass.CKA_KEY_TYPE, ObjectClass.CKK_RSA)
            ])
            if priv_keys:
                self.private_key = priv_keys[0]
                
            return True
        except Exception as e:
            logger.error(f"Error DNIe: {e}")
            return False
    
    def sign(self, data: bytes) -> Optional[bytes]:
        if not self.private_key or not self.session:
            return None
        try:
            mechanism = pkcs11_lib.Mechanism(pkcs11_lib.CKM_SHA256_RSA_PKCS, None)
            signature = self.session.sign(self.private_key, data, mechanism)
            return bytes(signature)
        except Exception as e:
            logger.error(f"Error firma: {e}")
            return None
    
    def get_fingerprint(self) -> Optional[str]:
        if not self.certificate:
            return None
        fp = self.certificate.fingerprint(hashes.SHA256())
        return fp.hex()
    
    def disconnect(self):
        if self.session:
            self.session.logout()
            self.session.closeSession()
