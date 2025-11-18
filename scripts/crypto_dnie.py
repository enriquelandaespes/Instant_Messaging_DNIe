#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instant Messaging with DNIe Identity
Implementación completa según A2_IMP_intro.pdf con integración DNIe via PKCS#11
(Exacto de código working: Clase manejo_datos para verify/cert, re-open sessions para priv/sign/ECDH)
"""

import asyncio
import contextlib
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
import re

# Verifica pyscard exacto de working
try:
    from smartcard.System import readers
except ImportError:
    print("La librería 'pyscard' no está instalada.")
    print("pip install pyscard")
    sys.exit()

import pkcs11  # Exacto: from pkcs11 import lib as pkcs11_lib, etc.
from pkcs11 import lib as pkcs11_lib, ObjectClass, Attribute, Mechanism, KeyType
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
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

# Configuración
SERVICE_TYPE = "_dni-im._udp.local."
CONTACTS_DB = Path("contacts.json")
MESSAGES_DB = Path("messages_signed.json")

# Exacto de working
PKCS11_LIB = r"C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll"
SLOT_INDEX = 0

# OID EC
SECP256R1_OID = b'\x06\x08\x2a\x86\x48\xce\x3d\x03\x01\x07'

# =====================================================================
# Detección exacta de working
# =====================================================================

def detectar_dnie():
    try:
        lista_lectores = readers()
        if not lista_lectores:
            return False
        lector = lista_lectores[0]
        conexion = lector.createConnection()
        try:
            conexion.connect()
            return True
        except Exception:
            return False
    except Exception:
        return False

# =====================================================================
# Clase exacta de working (sin cambios, solo import aliases)
# =====================================================================

class ManejoDNIeDatos:
    AES_KEY_SIZE = 32
    C_FILENAME = "C_value.bin"

    def __init__(self, pin: str):
        self.pin = pin
        self.token = self.obtener_token()
        self.cert = self.obtener_certificado_autenticacion()
        self.serial_hash = self.obtener_hash_serial()
        self.archivo_kdb = os.path.join(os.path.dirname(__file__), f"kdb_enc_{self.serial_hash}.bin")
        self.archivo_bd = os.path.join(os.path.dirname(__file__), f"Database_{self.serial_hash}.json.enc")
        self.archivo_C = os.path.join(os.path.dirname(__file__), self.C_FILENAME)
        self.k_db_cache = None
        self.inicializar_C()
        self.inicializar_kdb()

    def obtener_token(self):
        pkcs11 = pkcs11_lib(PKCS11_LIB)
        pkcs11.initialize()  # Añadido para OpenSC/DNIe compatibilidad
        slots = pkcs11.get_slots(token_present=True)
        if not slots:
            raise RuntimeError("No se encontró token DNIe.")
        return slots[self.SLOT_INDEX].get_token()

    def verificar_dnie(self, pin):
        try:
            token = self.obtener_token()
            print(f"Debug verify: Token label '{token.label}', slots {len(token.lib.get_slots())}")  # Debug
            with token.open(user_pin=pin) as session:  # Exacto
                print("Debug verify: Session opened OK")  # Debug
                return True
        except pkcs11.PKCS11Error as e:
            print(f"Debug verify error: {e}, rv={get_pkcs11_rv(e) if hasattr(e, 'rv') else 'N/A'}")  # Debug
            return False
        except Exception as e:
            print(f"Debug verify exception: {e}")  # Debug
            return False

    def obtener_certificado_autenticacion(self):
        with self.token.open(rw=True) as session:  # Exacto: rw=True, no PIN
            certificados = list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
            if not certificados:
                raise RuntimeError("No se encontró certificado en el DNIe.")
            der = certificados[0][Attribute.VALUE]  # Exacto [0]
            return x509.load_der_x509_certificate(der)

    def obtener_hash_serial(self) -> str:
        serial = str(self.cert.serial_number).encode('utf-8')
        h = sha256(serial).hexdigest()[:16]
        return h

    def inicializar_C(self):
        if os.path.exists(self.archivo_C):
            return
        C = os.urandom(8)
        with open(self.archivo_C, "wb") as f:
            f.write(C)

    def leer_C(self) -> bytes:
        with open(self.archivo_C, "rb") as f:
            data = f.read()
        if len(data) != 8:
            raise RuntimeError("Valor C inválido (longitud incorrecta).")
        return data

    def firmar_con_dni(self, data: bytes) -> bytes:
        with self.token.open(user_pin=self.pin) as session:
            keys = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
            if not keys:
                raise RuntimeError("No se encontró clave privada para firmar en el token.")
            priv = keys[1]  # Exacto de working: keys[1]
            try:
                signature = priv.sign(data, mechanism=Mechanism.SHA256_RSA_PKCS)
                return signature
            except Exception as e:
                raise RuntimeError(
                    "El DNIe no pudo firmar con el mecanismo de seguridad requerido (SHA256_RSA_PKCS). "
                    f"Asegúrate de que los drivers son correctos y el DNIe es compatible. Error original: {e}"
                ) from e

    # Resto de métodos de working no usados en IM (kdb, bd, etc.), kept por exactitud
    def inicializar_kdb(self):
        if os.path.exists(self.archivo_kdb):
            return
        k_db = os.urandom(self.AES_KEY_SIZE)
        C = self.leer_C()
        S = self.firmar_con_dni(C)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        K = sha256(S).digest()
        aesgcm = AESGCM(K)
        nonce = os.urandom(12)
        ct = aesgcm.encrypt(nonce, k_db, associated_data=None)
        with open(self.archivo_kdb, "wb") as f:
            f.write(nonce + ct)
        self.k_db_cache = k_db

    def descifrar_kdb(self) -> bytes:
        if self.k_db_cache is not None:
            return self.k_db_cache
        if not os.path.exists(self.archivo_kdb):
            raise RuntimeError("No existe la clave k_db cifrada para este DNI.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with open(self.archivo_kdb, "rb") as f:
            contenido = f.read()
        nonce = contenido[:12]
        ct = contenido[12:]
        C = self.leer_C()
        S = self.firmar_con_dni(C)
        K = sha256(S).digest()
        aesgcm = AESGCM(K)
        k_db = aesgcm.decrypt(nonce, ct, associated_data=None)
        self.k_db_cache = k_db
        return k_db

    # ... (resto: cargar_bd, guardar_bd, agregar_contraseña, etc., no usados, pero kept)

# =====================================================================
# Identity: Usa ManejoDNIeDatos para cert/verify, re-open para priv EC/RSA
# =====================================================================

@dataclass
class Identity:
    pkcs11: pkcs11_lib  # Lib global
    token: any  # Token
    static_pub_bytes: bytes
    alias: str
    cert_der: bytes  # Auth [0]
    signing_cert_der: bytes  # Sign [1]
    cert_auth: x509.Certificate
    serial_hash: str
    pin: str
    slot_index: int = SLOT_INDEX

def get_pkcs11_rv(e: Exception) -> int:
    if hasattr(e, 'rv'):
        return e.rv
    elif e.args and isinstance(e.args[0], int):
        return e.args[0]
    match = re.search(r'0x([0-9a-fA-F]+)', str(e))
    return int(match.group(1), 16) if match else 0x0

def load_identity_dnie(pin: str) -> Identity:
    """Usa ManejoDNIeDatos exacto para verify y cert; luego re-open para sign_cert y debug priv"""
    pkcs11 = None
    try:
        # Create lib with initialize (para evitar "module not associated")
        pkcs11 = pkcs11_lib(PKCS11_LIB)
        pkcs11.initialize()
        print(f"Debug load: Lib initialized from {PKCS11_LIB}")

        slots = pkcs11.get_slots(token_present=True)
        print(f"Debug load: Slots encontrados: {len(slots)}")
        if not slots:
            raise ValueError("No slots con token. Verifica OpenSC/DNIe drivers.")
        token = slots[SLOT_INDEX].get_token()
        print(f"Debug load: Token label: '{token.label}'")

        # Verify PIN exacto con debug
        md_temp = ManejoDNIeDatos("")  # Temp para verify
        md_temp.pkcs11 = pkcs11  # Share lib
        md_temp.token = token
        if not md_temp.verificar_dnie(pin):
            raise ValueError("PIN incorrecto o DNIe no válido. Ver debug arriba.")

        # Full cert exacto
        md = ManejoDNIeDatos(pin)  # Ahora con PIN, pero share lib si possible (no necesario)
        x509_auth = md.cert
        cn_attrs = x509_auth.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        alias = cn_attrs[0].value if cn_attrs else x509_auth.subject.rfc4514_string()[:20] or "Usuario DNIe"

        # Get DERs re-open (exacto style)
        with token.open(rw=True) as session:
            cert_objects = list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
            print(f"Debug load: Certs encontrados: {len(cert_objects)}")
            if len(cert_objects) < 2:
                raise ValueError("No suficientes certs (auth/sign). Ver debug.")
            auth_der = cert_objects[0][Attribute.VALUE]
            sign_der = cert_objects[1][Attribute.VALUE]  # Asume [1] sign RSA
            print("Debug load: Certs DER extraídos OK")

        # Debug priv keys (re-open with PIN)
        with token.open(user_pin=pin) as session_priv:
            priv_objects = list(session_priv.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
            print(f"Debug load: Priv keys encontradas: {len(priv_objects)}")
            if priv_objects:
                # Busca EC y RSA
                ec_count = sum(1 for p in priv_objects if p[Attribute.KEY_TYPE] == KeyType.EC)
                rsa_count = sum(1 for p in priv_objects if p[Attribute.KEY_TYPE] == KeyType.RSA)
                print(f"Debug load: EC priv: {ec_count}, RSA priv: {rsa_count}")
                # Test sign como working
                if len(priv_objects) > 1:
                    test_data = b"test"
                    try:
                        test_sig = priv_objects[1].sign(test_data, mechanism=Mechanism.SHA256_RSA_PKCS)
                        print(f"Debug load: Test sign OK (len {len(test_sig)})")
                    except Exception as te:
                        print(f"Debug load: Test sign fail: {te}")

        static_pub_bytes = x509_auth.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.UncompressedPoint
        )

        print(f"DNIe cargado exitosamente: {alias} (FP: {fingerprint(static_pub_bytes)[:8]})")
        return Identity(pkcs11, token, static_pub_bytes, alias, auth_der, sign_der, x509_auth, md.serial_hash, pin)

    except Exception as e:
        if pkcs11:
            pkcs11.finalize()
        rv = get_pkcs11_rv(e)
        msg = f"Error PKCS#11 (código {hex(rv) if rv else '0x0'}): {e}"
        if "No slots" in str(e):
            msg += ". Instala middleware DNIe oficial (eTPKCS11.dll) desde https://www.dnielectronico.es si OpenSC falla."
        elif "PIN incorrecto" in str(e):
            msg += ". Verifica PIN (4-8 dígitos)."
        elif "token" in str(e).lower():
            msg += ". Verifica lector/DNI insertado y drivers OpenSC (reinstala si DNIe 3.0)."
        raise ValueError(msg)

def fingerprint(static_pub_bytes: bytes) -> str:
    return sha256(static_pub_bytes).hexdigest()

def sign_messages(identity: Identity, data: bytes) -> bytes:
    """Exacto de working: re-open, get keys[1], sign SHA256_RSA_PKCS"""
    with identity.token.open(user_pin=identity.pin) as session:
        keys = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
        if not keys or len(keys) < 2:
            raise RuntimeError("No clave para firmar.")
        priv = keys[1]
        return priv.sign(data, mechanism=Mechanism.SHA256_RSA_PKCS)

def verify_messages(identity: Identity, data: bytes, signature: bytes) -> bool:
    try:
        x509_sign = x509.load_der_x509_certificate(identity.signing_cert_der)
        pk = x509_sign.public_key()
        pk.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False

# =====================================================================
# Derivar keys: Re-open session, get static_priv EC, derive
# =====================================================================

@dataclass
class SessionKeys:
    send_key: bytes
    recv_key: bytes

def hkdf_blake2s(ikm: bytes, info: bytes, length: int = 64) -> bytes:
    return HKDF(algorithm=hashes.BLAKE2s(32), length=length, salt=None, info=info).derive(ikm)

def derive_session_keys(identity: Identity, our_eph, peer_static_pub: bytes, peer_eph_pub: bytes, is_initiator: bool) -> SessionKeys:
    """Re-open session, get EC priv (primera EC), derive ECDH"""
    with identity.token.open(user_pin=identity.pin) as session:
        priv_objects = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY, Attribute.KEY_TYPE: KeyType.EC}))
        if not priv_objects:
            raise RuntimeError("No EC priv para ECDH.")
        static_priv = priv_objects[0]  # Primera EC auth

        mech_base = Mechanism(Mechanism.ECDH, param=SECP256R1_OID)
        # ss
        ss_mech = Mechanism(Mechanism.ECDH, param=peer_static_pub)
        ss = static_priv.derive_key(session, ss_mech)
        # se
        se_mech = Mechanism(Mechanism.ECDH, param=peer_eph_pub)
        se = static_priv.derive_key(session, se_mech)
        # es
        es_mech = Mechanism(Mechanism.ECDH, param=peer_static_pub)
        es = our_eph.derive_key(session, es_mech)
        # ee
        ee_mech = Mechanism(Mechanism.ECDH, param=peer_eph_pub)
        ee = our_eph.derive_key(session, ee_mech)

        mixed_dh = sorted([se, es], key=lambda b: b)
        key_material = ss + mixed_dh[0] + mixed_dh[1] + ee
        okm = hkdf_blake2s(key_material, b"dni-im-v1", 64)
        if is_initiator:
            return SessionKeys(okm[:32], okm[32:])
        return SessionKeys(okm[32:], okm[:32])

def encrypt(key: bytes, plaintext: bytes) -> Tuple[bytes, bytes]:
    aead = ChaCha20Poly1305(key)
    nonce = os.urandom(12)
    return nonce, aead.encrypt(nonce, plaintext, b"")

def decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, b"")

# =====================================================================
# Protocolo, Handshake, etc. (adapt gen eph en session)
# =====================================================================

class FrameType(IntEnum):
    HANDSHAKE = 0
    DATA = 1

HEADER = struct.Struct("!IIBBH")
CERT_HEADER = struct.Struct("!H")

def pack_frame(cid: int, stream_id: int, ftype: FrameType, payload: bytes) -> bytes:
    return HEADER.pack(cid, stream_id, ftype.value, 0, 0) + payload

def unpack_frame(data: bytes):
    cid, sid, ftype, _, _ = HEADER.unpack(data[:12])
    return cid, sid, FrameType(ftype), data[12:]

def build_handshake(identity: Identity) -> Tuple[any, bytes]:  # our_eph is temp priv
    with identity.token.open(user_pin=identity.pin) as session:
        # Gen eph EC pair
        ec_params = {Attribute.EC_PARAMS: SECP256R1_OID}
        pub_attr = {Attribute.PRIVATE: False, Attribute.TOKEN: False, Attribute.CLASS: ObjectClass.PUBLIC_KEY, Attribute.KEY_TYPE: KeyType.EC}
        priv_attr = {Attribute.PRIVATE: True, Attribute.SENSITIVE: True, Attribute.EXTRACTABLE: False, Attribute.TOKEN: False, Attribute.CLASS: ObjectClass.PRIVATE_KEY, Attribute.KEY_TYPE: KeyType.EC}
        mech = Mechanism(Mechanism.EC_KEY_PAIR_GEN, SECP256R1_OID)
        pub, priv = session.generate_keypair(mech, pub_attr, priv_attr)
        eph_pub = pub[Attribute.EC_POINT]

        fp = fingerprint(identity.static_pub_bytes)
        alias_key = hkdf_blake2s(fp.encode(), b"alias-key", 32)
        nonce, encrypted = encrypt(alias_key, identity.alias.encode("utf-8"))

        cert_len_bytes = CERT_HEADER.pack(len(identity.cert_der))
        payload = eph_pub + cert_len_bytes + identity.cert_der + nonce + encrypted
        return priv, payload  # priv para derive

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
        raise ValueError("Cert no EC.")
    static_pub = x509_cert.public_key().public_bytes(Encoding.Raw, PublicFormat.UncompressedPoint)
    return eph_pub, static_pub, nonce, enc_alias, cert_der

# =====================================================================
# Contactos, Session, UdpNode, Mdns, TUI (adaptados, sig 128 bytes para RSA1024 approx, pero 256 safe)
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
        return {fp: Contact(c["name"], fp, tuple(c["addr"]) if c.get("addr") else None) for fp, c in data.items()}
    except:
        return {}

def save_contacts(contacts: Dict[str, Contact]):
    data = {fp: {"name": c.name, "addr": list(c.addr) if c.addr else None} for fp, c in contacts.items()}
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
    pending_eph: Optional[any] = None

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
            
            alias_key = hkdf_blake2s(peer_fp.encode(), b"alias-key", 32)
            peer_alias = decrypt(alias_key, nonce, enc_alias).decode("utf-8")
            
            sess = self.sessions.get(cid)
            if sess and sess.is_init and not sess.complete and sess.pending_eph:
                keys = derive_session_keys(self.identity, sess.pending_eph, peer_static_pub, peer_eph_pub, True)
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
            
            keys = derive_session_keys(self.identity, our_eph, peer_static_pub, peer_eph_pub, False)
            sess.keys = keys
            sess.peer_static = peer_static_pub
            sess.peer_fp = peer_fp
            sess.complete = True
            
            self.transport.sendto(pack_frame(cid, 0, FrameType.HANDSHAKE, resp), addr)
            self.tui.render_contacts()
        except Exception as e:
            print(f"Debug handshake recv: {e}")  # Debug

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

# Resto MdnsService, ChatTUI similar (con load_messages sig=256, verify con signing_cert_der)
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
            SERVICE_TYPE, f"dni-im-{self.my_fp[:8]}.{SERVICE_TYPE}",
            addresses=[ip_bytes], port=self.port, properties={}, server=hostname,
        )
        await self.azc.async_register_service(self.info)
        
        self.browser = AsyncServiceBrowser(self.azc.zeroconf, SERVICE_TYPE, handlers=[self._on_change])

    def _on_change(self, zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added or self.my_fp[:8] in name:
            return
        asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            if not await info.async_request(zeroconf, 3000) or not info.addresses:
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
            json_part, signature = data[:-256], data[-256:]  # Safe para RSA
            if not verify_messages(self.identity, json_part, signature):
                print("Advertencia: Firma inválida en mensajes.")
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
            data_dict = {fp: [asdict(m) for m in msgs] for fp, msgs in self.chat_history.items()}
            json_str = json.dumps(data_dict).encode("utf-8")
            signature = sign_messages(self.identity, json_str)
            with MESSAGES_DB.open("wb") as f:
                f.write(json_str + signature)
        except Exception as e:
            print(f"Error guardando mensajes: {e}")

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
                    self.log(f"Mensaje desconocido: {msg}")

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
# Main: Detección, verify PIN con ManejoDNIeDatos, load, cleanup lib
# =====================================================================

async def main():
    if not detectar_dnie():
        print("No DNIe detectado.")
        sys.exit(1)
    print("DNIe detectado correctamente.")
    
    identity = None
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        pin = getpass(f"Introduce PIN DNIe (intento {attempt}/{max_attempts}): ")
        try:
            identity = load_identity_dnie(pin)
            break
        except ValueError as e:
            print(f"\n{e}")
            if attempt == max_attempts:
                print("Máximo intentos. Saliendo.")
                sys.exit(1)
    
    my_fp = fingerprint(identity.static_pub_bytes)
    contacts = load_contacts()
    
    loop = asyncio.get_running_loop()
    node = UdpNode(identity, contacts, None)
    port_str = input("Puerto UDP [6666]: ").strip() or "6666"
    port = int(port_str)
    transport, _ = await loop.create_datagram_endpoint(lambda: node, local_addr=("0.0.0.0", port))
    
    mdns = MdnsService(port, my_fp)
    await mdns.start(lambda addr: asyncio.create_task(node.connect_peer(addr)))
    
    tui = ChatTUI(node, contacts, identity.alias, identity)
    node.tui = tui
    
    try:
        await tui.run()
    finally:
        await mdns.stop()
        identity.pkcs11.finalize()  # Cleanup
        transport.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"Error inesperado: {e}")
        sys.exit(1)
