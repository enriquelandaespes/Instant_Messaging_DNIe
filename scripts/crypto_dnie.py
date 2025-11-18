#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instant Messaging with DNIe Identity
Implementación completa según A2_IMP_intro.pdf con integración DNIe via PKCS#11
(Sin fallback local: requiere DNIe funcional)
Integrado: Detección con pyscard + OpenSC PKCS#11 (de programa working) + Separación EC/RSA
"""

import asyncio
import contextlib
import datetime
import json
import os
import random
import shutil
import socket
import struct
import sys
import time
from dataclasses import dataclass, asdict
from enum import IntEnum
from hashlib import sha256
from getpass import getpass
from pathlib import Path
from typing import Dict, Tuple, Optional, List, Union
import re  # Para parsing RV

# Verifica si la librería pyscard está instalada (de programa working)
try:
    from smartcard.System import readers
    from smartcard.util import toHexString
except ImportError:
    print("La librería 'pyscard' no está instalada.")
    print("Para instalarla, ejecuta el siguiente comando en tu terminal:")
    print("pip install pyscard")
    sys.exit()

import pkcs11  # Import general; excepciones via PKCS11Error
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.asymmetric import padding

from zeroconf import ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo

from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

# Configuración según guión
SERVICE_TYPE = "_dni-im._udp.local."
CONTACTS_DB = Path("contacts.json")
MESSAGES_DB = Path("messages_signed.json")  # JSON de mensajes + firma

# Posibles DLL para DNIe (prioriza OpenSC de programa working + eTPKCS11)
POSSIBLE_DLLS = [
    r"C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll",  # OpenSC (working)
    r"C:\Program Files (x86)\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll",
    r"C:\Windows\System32\eTPKCS11.dll",
    r"C:\Windows\SysWOW64\eTPKCS11.dll",
    r"C:\Program Files\DNIe\Bin\eTPKCS11.dll",
    r"C:\Program Files (x86)\DNIe\Bin\eTPKCS11.dll",
    r"C:\DNIe\Bin\eTPKCS11.dll"
]

# OID para secp256r1 (ECDH)
SECP256R1_OID = b'\x06\x08\x2a\x86\x48\xce\x3d\x03\x01\x07'

# Códigos PKCS#11 para errores comunes
CKR_PIN_INCORRECT = 0xA0
CKR_PIN_LOCKED = 0xA4
CKR_TOKEN_NOT_PRESENT = 0x50
CKR_TOKEN_NOT_RECOGNIZED = 0x51
CKR_LIBRARY_FAILED_TO_LOAD = 0x20

# =====================================================================
# Detección DNIe con pyscard (integrado de programa working)
# =====================================================================

def detectar_dnie():
    """Función que detecta si un lector de tarjetas y un DNIe están conectados (de programa working)."""
    try:
        # Obtener la lista de lectores de tarjetas disponibles
        lista_lectores = readers()
        
        # Si no se detecta ningún lector, return False
        if not lista_lectores:
            return False

        # Obtener el primer lector disponible
        lector = lista_lectores[0]
        
        # Conectarse al lector
        conexion = lector.createConnection()
        
        try:
            # Intentar conectarse a la tarjeta (DNIe)
            conexion.connect()
            
            # Si la conexión es exitosa, se ha detectado un DNIe
            return True
            
        except Exception:
            # Si no se puede conectar, es probable que no haya un DNIe insertado
            return False
            
    except Exception:
        return False

# =====================================================================
# 1. IDENTIDAD con DNIe (requerida, sin fallback local) - Adaptado con EC/RSA + OpenSC
# =====================================================================

@dataclass
class Identity:
    static_priv: pkcs11.Key  # Clave privada auth EC del DNIe (para ECDH)
    static_pub_bytes: bytes  # Pub EC
    alias: str
    cert_der: bytes  # Cert auth EC DER para intercambio
    signing_priv: pkcs11.Key  # Clave privada sign RSA del DNIe (para firmas)
    signing_cert_der: bytes  # Cert sign RSA DER para verificación
    session: pkcs11.Session  # Sesión PKCS11 abierta
    lib: pkcs11.lib  # Lib para finalize
    dll_path: str  # Path de DLL usada

def get_pkcs11_rv(e: Exception) -> int:
    """Obtiene el código de retorno PKCS#11 de la excepción de forma segura"""
    if hasattr(e, 'rv'):
        return e.rv
    elif e.args and isinstance(e.args[0], int):
        return e.args[0]
    else:
        # Fallback: Parsea str(e) si contiene hex código
        match = re.search(r'0x([0-9a-fA-F]+)', str(e))
        return int(match.group(1), 16) if match else 0x0

def find_dll_path() -> str:
    """Busca y selecciona la primera DLL PKCS#11 DNIe existente (prioriza OpenSC)"""
    for dll in POSSIBLE_DLLS:
        if os.path.exists(dll):
            print(f"DLL encontrada: {dll}")
            return dll
    raise ValueError(f"Ninguna DLL PKCS#11 DNIe encontrada en paths comunes: {POSSIBLE_DLLS}. Instala OpenSC (https://opencsc-project.org) o middleware oficial desde https://www.dnielectronico.es/PortalDNIe/PS_Inicio.action")

def load_identity_dnie(pin: str) -> Identity:
    """Carga identidad desde DNIe via PKCS11; separa EC (auth) y RSA (sign); usa OpenSC/eTPKCS11"""
    lib = None
    session = None
    dll_path = None
    try:
        dll_path = find_dll_path()
        lib = pkcs11.lib(dll_path)
        lib.initialize()
        
        # Buscar slots con token (de programa working: get_slots(token_present=True))
        slots = lib.get_slots(token_present=True)
        print(f"Slots con token encontrados: {len(slots)}")
        if not slots:
            raise ValueError("No se encontró slot con token DNIe. Verifica lector y DNI insertado.")
        
        # Usar primer slot (SLOT_INDEX=0 como en working)
        token = slots[0].get_token()
        print(f"Token DNIe seleccionado: '{token.label}'")  # Debug label
        
        # Abrir sesión y login (de working: token.open(user_pin=pin))
        session = token.open(user_pin=pin)  # Usa PIN directamente como en working
        print("Login exitoso.")  # Debug
        
        # Obtener certificados X.509
        cert_template = {
            pkcs11.Attribute.CLASS: pkcs11.ObjectClass.CERTIFICATE,
            pkcs11.Attribute.CERTIFICATE_TYPE: 0  # X.509
        }
        cert_objects = session.get_objects(cert_template)
        print(f"Certificados X.509 encontrados ({len(cert_objects)}):")  # Debug
        for i, obj in enumerate(cert_objects):
            label = obj.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore')
            value_len = len(obj.get(pkcs11.Attribute.VALUE, b''))
            print(f"  {i}: Label: '{label}', Tamaño VALUE: {value_len} bytes")
        if not cert_objects:
            raise ValueError("Ningún certificado X.509 encontrado. Ver debug arriba; verifica estado del DNIe.")
        
        # Seleccionar auth_cert (EC): Keywords para auth
        auth_cert = None
        for obj in cert_objects:
            label = obj.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore').upper()
            if any(kw in label for kw in ['AUT', 'AUTH', 'AUTENTICACION', 'AUTHENTICATION', 'CERTAUT', 'DNI AUT']):
                # Verificar que es EC (OID en public key)
                try:
                    value = obj[pkcs11.Attribute.VALUE]
                    x509_temp = x509.load_der_x509_certificate(value)
                    if isinstance(x509_temp.public_key(), ec.EllipticCurvePublicKey):
                        auth_cert = obj
                        print(f"Cert auth EC seleccionado por label: '{label}'")
                        break
                except:
                    pass
        
        # Fallback auth: Mayor tamaño EC
        if not auth_cert:
            ec_certs = []
            for obj in cert_objects:
                try:
                    value = obj[pkcs11.Attribute.VALUE]
                    x509_temp = x509.load_der_x509_certificate(value)
                    if isinstance(x509_temp.public_key(), ec.EllipticCurvePublicKey):
                        ec_certs.append((obj, len(value)))
                except:
                    pass
            if ec_certs:
                auth_cert = max(ec_certs, key=lambda x: x[1])[0]
                label = auth_cert.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore')
                print(f"Fallback: Cert auth EC por tamaño: '{label}'")
            else:
                raise ValueError("Ningún certificado EC (auth) encontrado.")
        
        # Seleccionar sign_cert (RSA)
        sign_cert = None
        for obj in cert_objects:
            label = obj.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore').upper()
            if any(kw in label for kw in ['SIGN', 'FIRMA', 'SIGNATURE', 'CERTSIGN', 'DNI FIR']):
                # Verificar RSA
                try:
                    value = obj[pkcs11.Attribute.VALUE]
                    x509_temp = x509.load_der_x509_certificate(value)
                    if isinstance(x509_temp.public_key(), rsa.RSAPublicKey):
                        sign_cert = obj
                        print(f"Cert sign RSA seleccionado por label: '{label}'")
                        break
                except:
                    pass
        
        # Fallback sign: Primer RSA
        if not sign_cert:
            rsa_certs = []
            for obj in cert_objects:
                try:
                    value = obj[pkcs11.Attribute.VALUE]
                    x509_temp = x509.load_der_x509_certificate(value)
                    if isinstance(x509_temp.public_key(), rsa.RSAPublicKey):
                        rsa_certs.append(obj)
                except:
                    pass
            if rsa_certs:
                sign_cert = rsa_certs[0]
                label = sign_cert.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore')
                print(f"Fallback: Cert sign RSA: '{label}'")
            else:
                raise ValueError("Ningún certificado RSA (sign) encontrado.")
        
        # Obtener DER y alias (de auth_cert, como en working: certificados[0])
        if pkcs11.Attribute.VALUE not in auth_cert:
            raise ValueError("Cert auth no tiene VALUE.")
        cert_der = auth_cert[pkcs11.Attribute.VALUE]
        if len(cert_der) < 100:
            raise ValueError(f"VALUE auth demasiado corto ({len(cert_der)} bytes).")
        x509_auth = x509.load_der_x509_certificate(cert_der)
        cn_attrs = x509_auth.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        alias = cn_attrs[0].value if cn_attrs else x509_auth.subject.rfc4514_string()[:20] or "Usuario DNIe"
        
        static_pub_bytes = x509_auth.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.UncompressedPoint
        )
        
        signing_cert_der = sign_cert[pkcs11.Attribute.VALUE]
        
        # Obtener claves privadas: EC para auth, RSA para sign (de working: keys[1] para sign)
        # EC priv (auth)
        ec_priv_template = {
            pkcs11.Attribute.CLASS: pkcs11.ObjectClass.PRIVATE_KEY,
            pkcs11.Attribute.KEY_TYPE: pkcs11.KeyType.EC
        }
        ec_priv_objects = session.get_objects(ec_priv_template)
        print(f"Claves privadas EC encontradas ({len(ec_priv_objects)}):")
        for i, obj in enumerate(ec_priv_objects):
            label = obj.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore')
            obj_id = obj.get(pkcs11.Attribute.ID, b'')
            print(f"  {i}: Label: '{label}', ID: {obj_id}")
        if not ec_priv_objects:
            raise ValueError("Ninguna clave privada EC (auth) encontrada.")
        static_priv = ec_priv_objects[0]  # Primera EC como auth (ajustar si multiple)
        print(f"Clave auth EC: Label '{static_priv.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore')}'")
        
        # RSA priv (sign): De working, keys[1]
        rsa_priv_template = {
            pkcs11.Attribute.CLASS: pkcs11.ObjectClass.PRIVATE_KEY,
            pkcs11.Attribute.KEY_TYPE: pkcs11.KeyType.RSA
        }
        rsa_priv_objects = session.get_objects(rsa_priv_template)
        print(f"Claves privadas RSA encontradas ({len(rsa_priv_objects)}):")
        for i, obj in enumerate(rsa_priv_objects):
            label = obj.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore')
            obj_id = obj.get(pkcs11.Attribute.ID, b'')
            print(f"  {i}: Label: '{label}', ID: {obj_id}")
        if not rsa_priv_objects:
            raise ValueError("Ninguna clave privada RSA (sign) encontrada.")
        signing_priv = rsa_priv_objects[0]  # Primera RSA como sign (o [1] si coincide working)
        print(f"Clave sign RSA: Label '{signing_priv.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore')}'")
        
        # Verificar métodos
        if not hasattr(static_priv, 'derive_key'):
            raise ValueError("Clave auth EC no soporta ECDH.")
        if not hasattr(signing_priv, 'sign'):
            raise ValueError("Clave sign RSA no soporta sign.")
        
        print(f"DNIe cargado exitosamente: {alias} (FP EC: {fingerprint(static_pub_bytes)[:8]})")
        return Identity(static_priv, static_pub_bytes, alias, cert_der, signing_priv, signing_cert_der, session, lib, dll_path)
    
    except ValueError as ve:
        if "DLL" in str(ve):
            print("\n=== INSTRUCCIONES PARA INSTALAR DNIe MIDDLEWARE ===")
            print("1. Instala OpenSC: https://opencsc-project.org (elige Windows x64).")
            print("2. O middleware oficial: https://www.dnielectronico.es/PortalDNIe/PS_Inicio.action > 'Software DNI electrónico' > DNIe Middleware.")
            print("3. Instala con admin; reinicia PC.")
            print("4. Verifica DLL (busca 'opensc-pkcs11.dll' o 'eTPKCS11.dll').")
            print("5. Prueba con AutoFirma para confirmar DNIe.")
        raise ve
    except OSError as ose:
        if "No se puede encontrar el módulo" in str(ose):
            raise ValueError(f"DLL no cargable: {dll_path if dll_path else 'desconocida'}. Instala OpenSC o middleware DNIe.")
        raise ValueError(f"Error OS al cargar DLL ({dll_path}): {ose}")
    except pkcs11.PKCS11Error as e:
        rv = get_pkcs11_rv(e)
        error_msg = f"Error PKCS#11 (código {hex(rv)}): {e}"
        if rv == CKR_PIN_INCORRECT:
            raise ValueError(f"PIN incorrecto (código {hex(rv)}). Verifica PIN (4-8 dígitos).")
        elif rv == CKR_PIN_LOCKED:
            raise ValueError(f"PIN bloqueado (código {hex(rv)}). Contacta policía.")
        elif rv == CKR_LIBRARY_FAILED_TO_LOAD:
            error_msg += f". DLL cargada pero falló init: {dll_path}. Verifica arch (x64) y drivers."
        elif rv == CKR_TOKEN_NOT_PRESENT or rv == CKR_TOKEN_NOT_RECOGNIZED:
            error_msg = f"Token DNIe no presente (código {hex(rv)}): {e}. Verifica lector/DNI/middleware."
        if session:
            try:
                session.close()
            except:
                pass
        if lib:
            lib.finalize()
        raise ValueError(error_msg)
    except Exception as e:
        if session:
            try:
                session.close()
            except:
                pass
        if lib:
            lib.finalize()
        raise ValueError(f"Error cargando DNIe: {e}. Detalles: {str(e)}")

def fingerprint(static_pub_bytes: bytes) -> str:
    return sha256(static_pub_bytes).hexdigest()

def sign_messages(identity: Identity, data: bytes) -> bytes:
    """Firma con clave sign RSA del DNIe (SHA256_RSA_PKCS, como en working)"""
    mech = pkcs11.Mechanism(pkcs11.Mechanism.RSA_PKCS, hashes.SHA256())
    return identity.signing_priv.sign(data, mech)

def verify_messages(identity: Identity, data: bytes, signature: bytes) -> bool:
    """Verifica firma usando pub key del cert sign RSA"""
    try:
        x509_sign = x509.load_der_x509_certificate(identity.signing_cert_der)
        pk = x509_sign.public_key()
        pk.verify(
            signature, data, padding.PKCS1v15(), hashes.SHA256()
        )
        return True
    except InvalidSignature:
        return False

# Resto del código permanece igual (ECDH usa static_priv EC, etc.)
# ... (copiar derive_session_keys, hkdf, encrypt/decrypt, etc. sin cambios)

@dataclass
class SessionKeys:
    send_key: bytes
    recv_key: bytes

def hkdf_blake2s(ikm: bytes, info: bytes, length: int = 64) -> bytes:
    return HKDF(
        algorithm=hashes.BLAKE2s(32),
        length=length,
        salt=None,
        info=info,
    ).derive(ikm)

def derive_session_keys(identity: Identity, our_eph: pkcs11.Key, 
                       peer_static_pub: bytes, peer_eph_pub: bytes, is_initiator: bool) -> SessionKeys:
    """Deriva claves via ECDH en PKCS#11 (EC auth key)"""
    # ss: our_static x peer_static
    ss_mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDH, param=peer_static_pub)
    ss = identity.static_priv.derive_key(ss_mech, mechanism_param=SECP256R1_OID)
    
    # se: our_static x peer_eph
    se_mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDH, param=peer_eph_pub)
    se = identity.static_priv.derive_key(se_mech, mechanism_param=SECP256R1_OID)
    
    # es: our_eph x peer_static
    es_mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDH, param=peer_static_pub)
    es = our_eph.derive_key(es_mech, mechanism_param=SECP256R1_OID)
    
    # ee: our_eph x peer_eph
    ee_mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDH, param=peer_eph_pub)
    ee = our_eph.derive_key(ee_mech, mechanism_param=SECP256R1_OID)
    
    mixed_dh = sorted([se, es], key=lambda b: b)
    key_material = ss + mixed_dh[0] + mixed_dh[1] + ee
    okm = hkdf_blake2s(key_material, b"dni-im-v1", 64)
    
    if is_initiator:
        return SessionKeys(send_key=okm[:32], recv_key=okm[32:])
    else:
        return SessionKeys(send_key=okm[32:], recv_key=okm[:32])

def encrypt(key: bytes, plaintext: bytes) -> Tuple[bytes, bytes]:
    aead = ChaCha20Poly1305(key)
    nonce = os.urandom(12)
    return nonce, aead.encrypt(nonce, plaintext, b"")

def decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, b"")

# =====================================================================
# 3. PROTOCOLO UDP (sin cambios)
# =====================================================================

class FrameType(IntEnum):
    HANDSHAKE = 0
    DATA = 1

HEADER = struct.Struct("!IIBBH")  # CID, StreamID, Type, Flags, Reserved
CERT_HEADER = struct.Struct("!H")  # Longitud cert DER (2 bytes)

def pack_frame(cid: int, stream_id: int, ftype: FrameType, payload: bytes) -> bytes:
    return HEADER.pack(cid, stream_id, ftype.value, 0, 0) + payload

def unpack_frame(data: bytes):
    cid, sid, ftype, _, _ = HEADER.unpack(data[:12])
    return cid, sid, FrameType(ftype), data[12:]

# =====================================================================
# 4. HANDSHAKE (ajustado param ECDH)
# =====================================================================

def build_handshake(identity: Identity) -> Tuple[pkcs11.Key, bytes]:
    """Construye payload con eph_pub EC (temp), cert_der auth EC, nonce, enc_alias"""
    session = identity.session
    # Generar eph temp EC keypair
    ec_params = {pkcs11.Attribute.EC_PARAMS: SECP256R1_OID}
    domain = session.create_domain_parameters(pkcs11.KeyType.EC, ec_params, local=True)
    pub_attr = {pkcs11.Attribute.TOKEN: False, pkcs11.Attribute.PRIVATE: False}
    priv_attr = {pkcs11.Attribute.PRIVATE: True, pkcs11.Attribute.SENSITIVE: True, 
                 pkcs11.Attribute.EXTRACTABLE: False, pkcs11.Attribute.TOKEN: False}
    pub, priv = domain.generate_keypair(pub_attr, priv_attr)
    eph_pub = pub[pkcs11.Attribute.EC_POINT]  # 65 bytes
    
    # FP from static_pub EC
    fp = fingerprint(identity.static_pub_bytes)
    alias_key = hkdf_blake2s(fp.encode(), b"alias-key", 32)
    nonce, encrypted = encrypt(alias_key, identity.alias.encode("utf-8"))
    
    cert_len_bytes = CERT_HEADER.pack(len(identity.cert_der))
    payload = eph_pub + cert_len_bytes + identity.cert_der + nonce + encrypted
    return priv, payload

def parse_handshake(data: bytes):
    eph_pub_len = 65
    eph_pub = data[:eph_pub_len]
    cert_len = CERT_HEADER.unpack(data[eph_pub_len:eph_pub_len+2])[0]
    cert_start = eph_pub_len + 2
    cert_end = cert_start + cert_len
    cert_der = data[cert_start:cert_end]
    nonce_start = cert_end
    nonce = data[nonce_start:nonce_start + 12]
    enc_alias = data[nonce_start + 12:]
    
    x509_cert = x509.load_der_x509_certificate(cert_der)
    if not isinstance(x509_cert.public_key(), ec.EllipticCurvePublicKey):
        raise ValueError("Cert recibido no es EC (auth).")
    static_pub = x509_cert.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.UncompressedPoint
    )
    return eph_pub, static_pub, nonce, enc_alias, cert_der

# =====================================================================
# 5-10. Resto sin cambios (Contactos, Session, UdpNode, Mdns, TUI, Main)
# =====================================================================

@dataclass
class Contact:
    name: str
    fingerprint: str
    addr: Optional[Tuple[str, int]] = None

def load_contacts() -> Dict[str, Contact]:
    if not CONTACTS_DB.exists():
        return {}
    try:
        with CONTACTS_DB.open("r") as f:
            data = json.load(f)
        return {
            fp: Contact(c["name"], fp, tuple(c["addr"]) if c.get("addr") else None)
            for fp, c in data.items()
        }
    except:
        return {}

def save_contacts(contacts: Dict[str, Contact]):
    data = {
        fp: {"name": c.name, "addr": list(c.addr) if c.addr else None}
        for fp, c in contacts.items()
    }
    tmp = CONTACTS_DB.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f)
    shutil.move(str(tmp), str(CONTACTS_DB))

@dataclass
class Session:
    cid: int
    addr: Tuple[str, int]
    peer_fp: str
    peer_static: bytes
    keys: Optional[SessionKeys] = None
    next_stream: int = 1
    complete: bool = False
    is_init: bool = False
    pending_eph: Optional[pkcs11.Key] = None

class UdpNode(asyncio.DatagramProtocol):
    def __init__(self, identity: Identity, contacts: Dict[str, Contact], tui: 'ChatTUI'):
        self.identity = identity
        self.contacts = contacts
        self.tui = tui
        self.transport = None
        self.sessions: Dict[int, Session] = {}
        self.addr_to_cid: Dict[Tuple[str, int], int] = {}
        self.inbox = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        try:
            cid, sid, ftype, payload = unpack_frame(data)
            if ftype == FrameType.HANDSHAKE:
                asyncio.create_task(self._handle_handshake(cid, addr, payload))
            elif ftype == FrameType.DATA:
                asyncio.create_task(self._handle_data(cid, sid, payload))
        except:
            pass

    async def _handle_handshake(self, cid: int, addr, payload: bytes):
        try:
            peer_eph_pub, peer_static_pub, nonce, enc_alias, peer_cert_der = parse_handshake(payload)
            peer_fp = fingerprint(peer_static_pub)
            
            # Descifrar alias
            alias_key = hkdf_blake2s(peer_fp.encode(), b"alias-key", 32)
            peer_alias = decrypt(alias_key, nonce, enc_alias).decode("utf-8")
            
            # Si somos iniciador y recibimos respuesta
            sess = self.sessions.get(cid)
            if sess and sess.is_init and not sess.complete and sess.pending_eph:
                keys = derive_session_keys(
                    self.identity, sess.pending_eph, peer_static_pub, peer_eph_pub, True
                )
                sess.keys = keys
                sess.peer_static = peer_static_pub
                sess.peer_fp = peer_fp
                sess.complete = True
                
                if peer_fp not in self.contacts:
                    self.contacts[peer_fp] = Contact(peer_alias, peer_fp, addr)
                    self.tui.add_contact(peer_fp)
                else:
                    self.contacts[peer_fp].name = peer_alias
                    self.contacts[peer_fp].addr = addr
                save_contacts(self.contacts)
                self.tui.render_contacts()
                return
            
            if cid in self.sessions and self.sessions[cid].complete:
                return
            
            if peer_fp not in self.contacts:
                self.contacts[peer_fp] = Contact(peer_alias, peer_fp, addr)
                self.tui.add_contact(peer_fp)
            else:
                self.contacts[peer_fp].name = peer_alias
                self.contacts[peer_fp].addr = addr
            save_contacts(self.contacts)
            
            if cid not in self.sessions:
                sess = Session(cid, addr, peer_fp, peer_static_pub, is_init=False)
                self.sessions[cid] = sess
                self.addr_to_cid[addr] = cid
            
            our_eph, resp = build_handshake(self.identity)
            
            keys = derive_session_keys(
                self.identity, our_eph, peer_static_pub, peer_eph_pub, False
            )
            sess.keys = keys
            sess.peer_static = peer_static_pub
            sess.peer_fp = peer_fp
            sess.complete = True
            
            self.transport.sendto(pack_frame(cid, 0, FrameType.HANDSHAKE, resp), addr)
            self.tui.render_contacts()
        except Exception as e:
            pass

    async def _handle_data(self, cid: int, sid: int, payload: bytes):
        try:
            sess = self.sessions.get(cid)
            if not sess or not sess.keys or len(payload) < 12:
                return
            
            nonce, ciphertext = payload[:12], payload[12:]
            plaintext = decrypt(sess.keys.recv_key, nonce, ciphertext)
            msg = plaintext.decode("utf-8")
            
            await self.inbox.put((sess.peer_fp, msg))
        except:
            pass

    async def connect_peer(self, addr: Tuple[str, int]) -> Session:
        existing = self.addr_to_cid.get(addr)
        if existing and self.sessions[existing].complete:
            return self.sessions[existing]
        
        cid = random.randint(1, 2**32-1)
        sess = Session(cid, addr, "", b"", is_init=True)
        self.sessions[cid] = sess
        self.addr_to_cid[addr] = cid
        
        eph, payload = build_handshake(self.identity)
        sess.pending_eph = eph
        self.transport.sendto(pack_frame(cid, 0, FrameType.HANDSHAKE, payload), addr)
        
        deadline = time.time() + 5
        while not sess.complete and time.time() < deadline:
            await asyncio.sleep(0.05)
        
        return sess

    async def send_msg(self, contact: Contact, text: str):
        if not contact.addr:
            return
        
        sess = await self.connect_peer(contact.addr)
        if not sess.keys:
            return
        
        sid = sess.next_stream
        sess.next_stream += 1
        
        nonce, ciphertext = encrypt(sess.keys.send_key, text.encode("utf-8"))
        payload = nonce + ciphertext
        self.transport.sendto(pack_frame(sess.cid, sid, FrameType.DATA, payload), contact.addr)

class MdnsService:
    def __init__(self, port: int, my_fp: str):
        self.port = port
        self.my_fp = my_fp
        self.azc = None
        self.info = None
        self.browser = None
        self.on_peer = None
        self.seen = set()

    async def start(self, on_peer_cb):
        self.azc = AsyncZeroconf()
        self.on_peer = on_peer_cb
        
        local_ip = socket.gethostbyname(socket.gethostname())
        hostname = socket.gethostname() + ".local."
        ip_bytes = socket.inet_aton(local_ip)
        
        self.info = ServiceInfo(
            SERVICE_TYPE,
            f"dni-im-{self.my_fp[:8]}.{SERVICE_TYPE}",
            addresses=[ip_bytes],
            port=self.port,
            properties={},
            server=hostname,
        )
        await self.azc.async_register_service(self.info)
        
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf,
            SERVICE_TYPE,
            handlers=[self._on_change],
        )

    def _on_change(self, zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added:
            return
        if self.my_fp[:8] in name:
            return
        asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            if not await info.async_request(zeroconf, 3000):
                return
            if not info.addresses:
                return
            
            addr_str = socket.inet_ntoa(info.addresses[0])
            port = info.port
            addr = (addr_str, port)
            
            key = f"{addr_str}:{port}"
            if key in self.seen:
                return
            self.seen.add(key)
            
            if self.on_peer:
                self.on_peer(addr)
        except:
            pass

    async def stop(self):
        if self.browser:
            await self.browser.async_cancel()
        if self.azc and self.info:
            await self.azc.async_unregister_service(self.info)
            await self.azc.async_close()

@dataclass
class Message:
    sender: str
    text: str
    timestamp: str

class ChatTUI:
    def __init__(self, node: UdpNode, contacts: Dict[str, Contact], alias: str, identity: Identity):
        self.node = node
        self.contacts = contacts
        self.alias = alias
        self.identity = identity
        self.sel = 0
        self.recv_task = None
        self.refresh_task = None

        self.chat_history: Dict[str, List[Message]] = self.load_messages()

        self.contacts_area = TextArea(focusable=False, scrollbar=True, width=30)
        self.msgs_area = TextArea(focusable=False, scrollbar=True, wrap_lines=True)
        self.input_area = TextArea(height=3, prompt="> ")

        self.left = Frame(self.contacts_area, title="Chats")
        self.center = Frame(self.msgs_area, title=alias)
        self.bottom = Frame(self.input_area, title="Input")

        root = HSplit([VSplit([self.left, self.center], padding=1), self.bottom])

        kb = KeyBindings()

        @kb.add("up")
        def _(e):
            self.sel = max(0, self.sel - 1)
            self.render_chat()

        @kb.add("down")
        def _(e):
            if self.contacts:
                self.sel = min(len(self.contacts) - 1, self.sel + 1)
            self.render_chat()

        @kb.add("enter")
        def _(e):
            asyncio.create_task(self._on_enter())

        @kb.add("c-c")
        def _(e):
            e.app.exit()

        self.app = Application(
            layout=Layout(root),
            key_bindings=kb,
            full_screen=True,
            style=Style.from_dict({"frame.border": "ansiblue", "frame.title": "ansigreen"}),
            refresh_interval=0.2,
        )

    def add_contact(self, fp: str):
        if fp not in self.chat_history:
            self.chat_history[fp] = []

    def render_contacts(self):
        lines = []
        for i, fp in enumerate(self.contacts):
            c = self.contacts[fp]
            p = "➤ " if i == self.sel else "  "
            lines.append(f"{p}{c.name}")
        self.contacts_area.text = "\n".join(lines) if lines else "Sin contactos"

    def render_chat(self):
        self.render_contacts()
        if not self.contacts:
            self.msgs_area.text = "Selecciona un chat o espera peers..."
            return
        
        fp_list = list(self.contacts.keys())
        selected_fp = fp_list[self.sel]
        history = self.chat_history.get(selected_fp, [])
        
        lines = []
        chat_width = 60
        for msg in history:
            if msg.sender == self.alias:
                padded = f"{msg.text:<{chat_width-10}} {msg.sender} ({msg.timestamp})"
                lines.append(padded)
            else:
                lines.append(f"{msg.sender} ({msg.timestamp}): {msg.text}")
        
        self.msgs_area.text = "\n".join(lines)
        self.app.invalidate()

    def add_message(self, fp: str, sender: str, text: str):
        timestamp = time.strftime("%H:%M")
        msg = Message(sender, text, timestamp)
        if fp not in self.chat_history:
            self.chat_history[fp] = []
        self.chat_history[fp].append(msg)
        if len(self.chat_history[fp]) > 20:
            self.chat_history[fp] = self.chat_history[fp][-20:]
        self.save_messages()
        fp_list = list(self.contacts.keys())
        if fp_list and fp == fp_list[self.sel]:
            self.render_chat()

    def load_messages(self) -> Dict[str, List[Message]]:
        if not MESSAGES_DB.exists():
            return {}
        try:
            with MESSAGES_DB.open("rb") as f:
                data = f.read()
            json_part, signature = data[:-256], data[-256:]  # RSA sig ~256 bytes
            if not verify_messages(self.identity, json_part, signature):
                print("Advertencia: Firma de mensajes inválida, cargando vacíos")
                return {}
            history_dict = json.loads(json_part.decode("utf-8"))
            return {
                fp: [Message(m["sender"], m["text"], m["timestamp"]) for m in msgs]
                for fp, msgs in history_dict.items()
            }
        except Exception as e:
            print(f"Error cargando mensajes: {e}")
            return {}

    def save_messages(self):
        try:
            data_dict = {
                fp: [asdict(m) for m in msgs]
                for fp, msgs in self.chat_history.items()
            }
            json_str = json.dumps(data_dict).encode("utf-8")
            signature = sign_messages(self.identity, json_str)
            with MESSAGES_DB.open("wb") as f:
                f.write(json_str + signature)
        except Exception as e:
            pass

    def log(self, m: str):
        if len(self.contacts) == 0:
            self.msgs_area.text += "\n" + m if self.msgs_area.text else m
        self.app.invalidate()

    async def _on_enter(self):
        text = self.input_area.text.strip()
        self.input_area.buffer.reset()
        
        if not text:
            return
        
        if text.startswith("/new "):
            try:
                _, name, ip_port = text.split(None, 2)
                ip, port = ip_port.split(":")
                addr = (ip, int(port))
                fake_fp = f"manual-{ip}-{port}"
                self.contacts[fake_fp] = Contact(name, fake_fp, addr)
                self.add_contact(fake_fp)
                save_contacts(self.contacts)
                self.render_chat()
                self.log(f"* {name} añadido")
            except:
                self.log("* Uso: /new nombre ip:puerto")
            return
        
        if not self.contacts:
            self.log("* Sin contactos")
            return
        
        fp_list = list(self.contacts.keys())
        selected_fp = fp_list[self.sel]
        contact = self.contacts[selected_fp]
        
        self.add_message(selected_fp, self.alias, text)
        
        await self.node.send_msg(contact, text)
        self.render_chat()

    async def run(self):
        async def receiver():
            while True:
                fp, msg = await self.node.inbox.get()
                if fp in self.contacts:
                    self.add_message(fp, self.contacts[fp].name, msg)
                else:
                    self.log(f"Mensaje de peer desconocido: {msg}")

        async def refresher():
            while True:
                await asyncio.sleep(1.0)
                self.render_contacts()
                if self.app.is_running:
                    self.app.invalidate()

        self.recv_task = asyncio.create_task(receiver())
        self.refresh_task = asyncio.create_task(refresher())
        self.render_chat()
        try:
            await self.app.run_async()
        finally:
            self.save_messages()
            if self.recv_task:
                self.recv_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.recv_task
            if self.refresh_task:
                self.refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.refresh_task

# =====================================================================
# 10. MAIN (añadido detección DNIe antes de PIN)
# =====================================================================

async def main():
    # Detección DNIe integrada (de working)
    if not detectar_dnie():
        print("No se detectó lector de tarjetas o DNIe insertado. Verifica hardware y reintenta.")
        sys.exit(1)
    print("DNIe detectado correctamente.")
    
    identity = None
    max_attempts = 3
    attempt = 1
    while attempt <= max_attempts:
        try:
            pin = getpass(f"Introduce PIN DNIe (intento {attempt}/{max_attempts}): ")
            identity = load_identity_dnie(pin)
            break
        except ValueError as e:
            print(f"\n{e}")
            if "PIN incorrecto" in str(e) and attempt < max_attempts:
                print("Intenta de nuevo...")
                attempt += 1
                continue
            elif "PIN bloqueado" in str(e):
                print("DNIe bloqueado. Contacta con la policía para desbloqueo.")
                sys.exit(1)
            else:
                break
        except KeyboardInterrupt:
            sys.exit(0)
    if not identity:
        print(f"\nMáximo de intentos ({max_attempts}) alcanzado o error persistente. Saliendo.")
        sys.exit(1)
    
    my_fp = fingerprint(identity.static_pub_bytes)
    contacts = load_contacts()
    
    loop = asyncio.get_running_loop()
    node = UdpNode(identity, contacts, None)
    port_str = input("Puerto UDP [6666]: ").strip() or "6666"
    port = int(port_str)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: node,
        local_addr=("0.0.0.0", port),
    )
    
    mdns = MdnsService(port, my_fp)
    await mdns.start(lambda addr: asyncio.create_task(node.connect_peer(addr)))
    
    tui = ChatTUI(node, contacts, identity.alias, identity)
    node.tui = tui
    
    try:
        await tui.run()
    finally:
        await mdns.stop()
        identity.session.close()
        identity.lib.finalize()
        transport.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"\n=== ERROR INESPERADO ===")
        print(f"Detalles: {e}")
        print(f"Tipo: {type(e).__name__}")
        print(f"Args: {e.args}")
        sys.exit(1)
