"""Gestión de claves X25519 para Noise Protocol"""
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives import serialization
import os

class KeyManager:
    def __init__(self):
        self.private_key = None
        self.public_key = None
        
    def generate_keypair(self):
        self.private_key = X25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        
    def get_public_key_bytes(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
    
    def get_private_key_bytes(self) -> bytes:
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
    
    def load_or_generate(self, path: str = "static_key.priv"):
        if os.path.exists(path):
            with open(path, 'rb') as f:
                key_bytes = f.read()
                self.private_key = X25519PrivateKey.from_private_bytes(key_bytes)
                self.public_key = self.private_key.public_key()
        else:
            self.generate_keypair()
            self.save(path)
    
    def save(self, path: str):
        with open(path, 'wb') as f:
            f.write(self.get_private_key_bytes())
