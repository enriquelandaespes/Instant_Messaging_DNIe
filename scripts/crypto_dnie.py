#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instant Messaging with DNIe Identity
Implementación completa según A2_IMP_intro.pdf con integración DNIe via PKCS#11
(Sin fallback local: requiere DNIe funcional)
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

import pkcs11  # Import general; excepciones via PKCS11Error
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

# Configuración según guión
SERVICE_TYPE = "_dni-im._udp.local."
CONTACTS_DB = Path("contacts.json")
MESSAGES_DB = Path("messages_signed.json")  # JSON de mensajes + firma

# PKCS#11 DLL para DNIe (ajustar según instalación; común en Windows/Linux)
PKCS11_DLL = r"C:\Windows\System32\eTPKCS11.dll"  # Windows; para Linux: "/usr/lib/libeTPKCS11.so" o similar

# OID para secp256r1 (ECDH)
SECP256R1_OID = b'\x06\x08\x2a\x86\x48\xce\x3d\x03\x01\x07'

# Códigos PKCS#11 para errores comunes
CKR_PIN_INCORRECT = 0xA0
CKR_PIN_LOCKED = 0xA4
CKR_TOKEN_NOT_PRESENT = 0x50
CKR_TOKEN_NOT_RECOGNIZED = 0x51

# =====================================================================
# 1. IDENTIDAD con DNIe (requerida, sin fallback local)
# =====================================================================

@dataclass
class Identity:
    static_priv: pkcs11.Key  # Clave privada auth del DNIe
    static_pub_bytes: bytes
    alias: str
    cert_der: bytes  # Certificado DER para intercambio
    signing_priv: pkcs11.Key  # Clave privada sign del DNIe
    session: pkcs11.Session  # Sesión PKCS11 abierta
    lib: pkcs11.lib  # Lib para finalize

def load_identity_dnie(pin: str) -> Identity:
    """Carga identidad desde DNIe via PKCS11; falla si no detecta DNIe"""
    lib = None
    session = None
    try:
        lib = pkcs11.lib(PKCS11_DLL)
        lib.initialize()
        
        # Buscar token DNIe (labels comunes: 'DNIe', 'DNI', 'DNI electrónico')
        tokens = lib.get_tokens()
        print(f"Tokens encontrados: {[t.label for t in tokens]}")  # Debug: muestra labels
        dni_token = None
        for token in tokens:
            label_upper = token.label.upper()
            if any(kw in label_upper for kw in ['DNI', 'DNIE', 'ELECTRÓNICO']):
                dni_token = token
                print(f"Token DNIe seleccionado: {token.label}")  # Debug
                break
        if not dni_token:
            raise ValueError(f"No se encontró token DNIe. Tokens disponibles: {[t.label for t in tokens]}. Verifica lector y DNI insertado.")
        
        # Abrir sesión y login
        session = dni_token.open()
        try:
            session.login(pkcs11.UserType.USER, pin)
            print("Login exitoso.")  # Debug
        except pkcs11.PKCS11Error as e:
            if e.rv == CKR_PIN_INCORRECT:
                raise ValueError(f"PIN incorrecto (código {hex(e.rv)}). Verifica el PIN de autenticación del DNIe (4-8 dígitos).")
            elif e.rv == CKR_PIN_LOCKED:
                raise ValueError(f"PIN bloqueado (código {hex(e.rv)}). El DNIe está bloqueado tras múltiples intentos erróneos; contacta policía para desbloqueo.")
            else:
                raise ValueError(f"Error en login (código {hex(e.rv)}): {e}. Verifica PIN, estado del DNIe y drivers.")
        
        # Obtener certificados (labels comunes: 'CertAut', 'Certificado de Autenticación DNIe', 'CertSign', 'Certificado de Firma DNIe')
        cert_objects = session.get_objects({pkcs11.Attribute.CLASS: pkcs11.ObjectClass.CERTIFICATE})
        print(f"Certificados encontrados ({len(cert_objects)}):")  # Debug
        for obj in cert_objects:
            label = obj.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore')
            print(f"  - Label: '{label}', ID: {obj[pkcs11.Attribute.ID] if pkcs11.Attribute.ID in obj else 'N/A'}")  # Debug
        auth_cert = None
        signing_cert = None
        for obj in cert_objects:
            label = obj.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore').upper()
            if any(kw in label for kw in ['AUT', 'AUTH', 'AUTENTICACION', 'AUTHENTICATION']):
                auth_cert = obj
                print(f"Cert auth seleccionado: {label}")  # Debug
            if any(kw in label for kw in ['SIGN', 'FIRMA', 'SIGNATURE']):
                signing_cert = obj
                print(f"Cert sign seleccionado: {label}")  # Debug
        
        if not auth_cert:
            raise ValueError("Certificado de autenticación no encontrado. Labels comunes: 'CertAut', 'Certificado de Autenticación'. Ver debug arriba.")
        
        cert_der = auth_cert[pkcs11.Attribute.VALUE]
        x509_cert = x509.load_der_x509_certificate(cert_der)
        cn_attrs = x509_cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        alias = cn_attrs[0].value if cn_attrs else x509_cert.subject.rfc4514_string()[:20] or "Usuario DNIe"
        
        static_pub_bytes = x509_cert.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.UncompressedPoint
        )
        
        # Obtener claves privadas (buscar por label o ID común)
        priv_objects = session.get_objects({pkcs11.Attribute.CLASS: pkcs11.ObjectClass.PRIVATE_KEY})
        print(f"Claves privadas encontradas ({len(priv_objects)}):")  # Debug
        for obj in priv_objects:
            label = obj.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore')
            print(f"  - Label: '{label}', ID: {obj[pkcs11.Attribute.ID] if pkcs11.Attribute.ID in obj else 'N/A'}")  # Debug
        auth_priv = None
        signing_priv = None
        for obj in priv_objects:
            label = obj.get(pkcs11.Attribute.LABEL, b'').decode('utf-8', errors='ignore').upper()
            if any(kw in label for kw in ['AUT', 'AUTH', 'AUTENTICACION', 'AUTHENTICATION']):
                auth_priv = obj
                print(f"Clave auth seleccionada: {label}")  # Debug
            if any(kw in label for kw in ['SIGN', 'FIRMA', 'SIGNATURE']):
                signing_priv = obj
                print(f"Clave sign seleccionada: {label}")  # Debug
        
        # Fallback por ID si labels no coinciden (común: ID b'01' para auth, b'02' para sign)
        if not auth_priv:
            for obj in priv_objects:
                obj_id = obj.get(pkcs11.Attribute.ID, b'')
                if obj_id == b'01':  # ID típico para auth
                    auth_priv = obj
                    print("Clave auth por ID b'01'")
                    break
        if not signing_priv:
            for obj in priv_objects:
                obj_id = obj.get(pkcs11.Attribute.ID, b'')
                if obj_id == b'02':  # ID típico para sign
                    signing_priv = obj
                    print("Clave sign por ID b'02'")
                    break
        
        if not auth_priv:
            raise ValueError("Clave privada de autenticación no encontrada. Ver debug arriba; ajusta keywords si needed.")
        
        if not signing_priv:
            raise ValueError("Clave privada de firma no encontrada. Ver debug arriba; ajusta keywords si needed.")
        
        print(f"DNIe cargado exitosamente: {alias} (FP: {fingerprint(static_pub_bytes)[:8]})")
        return Identity(auth_priv, static_pub_bytes, alias, cert_der, signing_priv, session, lib)
    
    except pkcs11.PKCS11Error as e:
        if session:
            try:
                session.logout()
                session.close()
            except:
                pass
        if lib:
            lib.finalize()
        if e.rv == CKR_TOKEN_NOT_PRESENT or e.rv == CKR_TOKEN_NOT_RECOGNIZED:
            raise ValueError(f"Token DNIe no presente/reconocido (código {hex(e.rv)}): {e}. Verifica lector, DNI insertado y drivers (instala middleware FNMT).")
        else:
            raise ValueError(f"Error PKCS#11 (código {hex(e.rv)}): {e}. Verifica DLL y drivers.")
    except Exception as e:
        if session:
            try:
                session.logout()
                session.close()
            except:
                pass
        if lib:
            lib.finalize()
        raise ValueError(f"Error cargando DNIe: {e}. Verifica DLL, drivers y PIN. Detalles: {str(e)}")

def fingerprint(static_pub_bytes: bytes) -> str:
    return sha256(static_pub_bytes).hexdigest()

def sign_messages(identity: Identity, data: bytes) -> bytes:
    """Firma con clave signing del DNIe (ECDSA-SHA256)"""
    mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDSA, pkcs11.util.ec.ECDSA_SHA256)
    return identity.signing_priv.sign(data, mech)

def verify_messages(identity: Identity, data: bytes, signature: bytes) -> bool:
    """Verifica firma usando pub key del cert auth (asume signing same curve)"""
    try:
        x509_cert = x509.load_der_x509_certificate(identity.cert_der)
        pk = x509_cert.public_key()
        pk.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False

# =====================================================================
# 2. CRIPTOGRAFÍA (ECDH con P-256 + Noise IK via PKCS#11)
# =====================================================================

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
    """Deriva claves via ECDH en PKCS#11 (requerido DNIe)"""
    # ss: our_static x peer_static
    ss_mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDH, param=peer_static_pub)
    ss = identity.static_priv.derive_key(ss_mech)
    
    # se: our_static x peer_eph
    se_mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDH, param=peer_eph_pub)
    se = identity.static_priv.derive_key(se_mech)
    
    # es: our_eph x peer_static
    es_mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDH, param=peer_static_pub)
    es = our_eph.derive_key(es_mech)
    
    # ee: our_eph x peer_eph
    ee_mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDH, param=peer_eph_pub)
    ee = our_eph.derive_key(ee_mech)
    
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
# 3. PROTOCOLO UDP
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
# 4. HANDSHAKE con intercambio de certificados
# =====================================================================

def build_handshake(identity: Identity) -> Tuple[pkcs11.Key, bytes]:
    """Construye payload con eph_pub (temp keypair en session), cert_der, nonce, enc_alias"""
    session = identity.session
    # Generar eph temp keypair en session (no persistente)
    ec_params = {pkcs11.Attribute.EC_PARAMS: SECP256R1_OID}
    domain = session.create_domain_parameters(pkcs11.KeyType.EC, ec_params, local=True)
    pub_attr = {pkcs11.Attribute.TOKEN: False, pkcs11.Attribute.PRIVATE: False}
    priv_attr = {pkcs11.Attribute.PRIVATE: True, pkcs11.Attribute.SENSITIVE: True, 
                 pkcs11.Attribute.EXTRACTABLE: False, pkcs11.Attribute.TOKEN: False}
    pub, priv = domain.generate_keypair(pub_attr, priv_attr)
    eph_pub = pub[pkcs11.Attribute.EC_POINT]  # Raw point bytes (65 bytes uncompressed)
    
    # FP from static_pub
    fp = fingerprint(identity.static_pub_bytes)
    alias_key = hkdf_blake2s(fp.encode(), b"alias-key", 32)
    nonce, encrypted = encrypt(alias_key, identity.alias.encode("utf-8"))
    
    cert_len_bytes = CERT_HEADER.pack(len(identity.cert_der))
    payload = eph_pub + cert_len_bytes + identity.cert_der + nonce + encrypted
    return priv, payload  # eph_sk is priv pkcs11.Key

def parse_handshake(data: bytes):
    """Parse payload, extrae cert, static_pub from cert"""
    eph_pub_len = 65  # Uncompressed P-256: 0x04 + 32x + 32y
    eph_pub = data[:eph_pub_len]
    cert_len = CERT_HEADER.unpack(data[eph_pub_len:eph_pub_len+2])[0]
    cert_start = eph_pub_len + 2
    cert_end = cert_start + cert_len
    cert_der = data[cert_start:cert_end]
    nonce_start = cert_end
    nonce = data[nonce_start:nonce_start + 12]
    enc_alias = data[nonce_start + 12:]
    
    x509_cert = x509.load_der_x509_certificate(cert_der)
    static_pub = x509_cert.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.UncompressedPoint
    )
    return eph_pub, static_pub, nonce, enc_alias, cert_der

# =====================================================================
# 5. CONTACTOS
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

# =====================================================================
# 6. SESIÓN
# =====================================================================

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
    pending_eph: Optional[pkcs11.Key] = None  # Siempre PKCS11

# =====================================================================
# 7. NODO UDP
# =====================================================================

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
                # Derivar claves como iniciador
                keys = derive_session_keys(
                    self.identity, sess.pending_eph, peer_static_pub, peer_eph_pub, True
                )
                sess.keys = keys
                sess.peer_static = peer_static_pub
                sess.peer_fp = peer_fp
                sess.complete = True
                
                # Actualizar contacto
                if peer_fp not in self.contacts:
                    self.contacts[peer_fp] = Contact(peer_alias, peer_fp, addr)
                    self.tui.add_contact(peer_fp)
                else:
                    self.contacts[peer_fp].name = peer_alias
                    self.contacts[peer_fp].addr = addr
                save_contacts(self.contacts)
                self.tui.render_contacts()
                return
            
            # Si ya existe sesión completa, ignorar
            if cid in self.sessions and self.sessions[cid].complete:
                return
            
            # Actualizar/crear contacto
            if peer_fp not in self.contacts:
                self.contacts[peer_fp] = Contact(peer_alias, peer_fp, addr)
                self.tui.add_contact(peer_fp)
            else:
                self.contacts[peer_fp].name = peer_alias
                self.contacts[peer_fp].addr = addr
            save_contacts(self.contacts)
            
            # Crear sesión como responder
            if cid not in self.sessions:
                sess = Session(cid, addr, peer_fp, peer_static_pub, is_init=False)
                self.sessions[cid] = sess
                self.addr_to_cid[addr] = cid
            
            # Responder con nuestro handshake (genera eph en session)
            our_eph, resp = build_handshake(self.identity)
            
            # Derivar claves como responder
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

# =====================================================================
# 8. mDNS
# =====================================================================

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

# =====================================================================
# 9. TUI
# =====================================================================

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
            json_part, signature = data[:-64], data[-64:]
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
# 10. MAIN (requiere DNIe; sale si falla, con múltiples intentos PIN)
# =====================================================================

async def main():
    identity = None
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            pin = getpass(f"Introduce PIN DNIe (intento {attempt}/{max_attempts}): ")
            identity = load_identity_dnie(pin)
            break
        except ValueError as e:
            print(f"\n{e}")
            if "PIN incorrecto" in str(e) and attempt < max_attempts:
                print("Intenta de nuevo...")
                continue
            elif "PIN bloqueado" in str(e):
                print("DNIe bloqueado. Contacta con la policía para desbloqueo.")
                sys.exit(1)
            else:
                raise
    else:
        print(f"\nMáximo de intentos ({max_attempts}) alcanzado. Saliendo.")
        sys.exit(1)
    
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"\n=== ERROR INESPERADO EN DNIe ===")
        print(f"Detalles: {e}")
        print("Contacta soporte o verifica instalación PKCS#11.")
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
        identity.session.logout()
        identity.session.close()
        identity.lib.finalize()
        transport.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)