#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instant Messaging with DNIe Identity
Implementación completa según A2_IMP_intro.pdf con integración DNIe via PKCS#11
(Código base funcional + acceso DNIe exacto de tu working con from pkcs11 import lib)
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
from typing import Dict, Tuple, Optional, List

# Detección pyscard exacta de tu working
try:
    from smartcard.System import readers
except ImportError:
    print("La librería 'pyscard' no está instalada.")
    print("pip install pyscard")
    sys.exit()

# Importación exacta de tu working (sin aliases que causen SLOT_INDEX error)
from pkcs11 import lib as pkcs11_lib, ObjectClass, Attribute, Mechanism
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

# DLL y SLOT exacto de tu working
PKCS11_LIB = r"C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll"
SLOT_INDEX = 0  # Variable global, no atributo de clase

# OID EC P-256
SECP256R1_OID = b'\x06\x08\x2a\x86\x48\xce\x3d\x03\x01\x07'

# =====================================================================
# Detección DNIe exacta de tu working
# =====================================================================

def detectar_dnie():
    """Función que detecta si un lector de tarjetas y un DNIe están conectados."""
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
# Acceso DNIe EXACTO de tu working (obtener_token, verificar, cert, firmar)
# =====================================================================

def obtener_token():
    """Exacto de tu working: pkcs11 = pkcs11_lib(PKCS11_LIB), get_slots, return token"""
    pkcs11 = pkcs11_lib(PKCS11_LIB)
    slots = pkcs11.get_slots(token_present=True)
    if not slots:
        raise RuntimeError("No se encontró token DNIe.")
    return slots[SLOT_INDEX].get_token()

def verificar_dnie(pin):
    """Exacto de tu working: token.open(user_pin=pin) en context manager"""
    try:
        token = obtener_token()
        with token.open(user_pin=pin):  # Exacto
            return True
    except Exception:
        return False

def obtener_certificado_autenticacion():
    """Exacto de tu working: token.open(rw=True) sin PIN, get_objects CERTIFICATE, [0], x509.load_der"""
    token = obtener_token()
    with token.open(rw=True) as session:  # Exacto: rw=True, no PIN para leer certs
        certificados = list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
        if not certificados:
            raise RuntimeError("No se encontró certificado en el DNIe.")
        der = certificados[0][Attribute.VALUE]  # Exacto: [0] es auth
        return x509.load_der_x509_certificate(der)

def firmar_con_dni(pin: str, data: bytes) -> bytes:
    """Exacto de tu working: token.open(user_pin=pin), get PRIVATE_KEY, keys[1], sign SHA256_RSA_PKCS"""
    token = obtener_token()
    with token.open(user_pin=pin) as session:
        keys = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
        if not keys:
            raise RuntimeError("No se encontró clave privada para firmar en el token.")
        
        priv = keys[1]  # Exacto de tu working: keys[1] es signing key
        
        try:
            # Exacto: mechanism=Mechanism.SHA256_RSA_PKCS
            signature = priv.sign(data, mechanism=Mechanism.SHA256_RSA_PKCS)
            return signature
        except Exception as e:
            raise RuntimeError(
                f"El DNIe no pudo firmar con SHA256_RSA_PKCS. Error: {e}"
            ) from e

# =====================================================================
# 1. IDENTIDAD (simplificada, usando funciones exactas de working)
# =====================================================================

@dataclass
class Identity:
    static_priv: any  # Clave EC auth (keys[0])
    static_pub_bytes: bytes
    alias: str
    cert_der: bytes
    signing_priv: any  # Clave RSA sign (keys[1])
    signing_cert_der: bytes
    pin: str
    token: any

def load_identity_dnie(pin: str) -> Identity:
    """Carga identidad usando funciones exactas de tu working"""
    try:
        # Verificar PIN exacto
        if not verificar_dnie(pin):
            raise ValueError("PIN incorrecto o DNIe no válido.")
        print("PIN verificado correctamente.")
        
        # Obtener certificado auth [0] exacto
        x509_auth = obtener_certificado_autenticacion()
        cn_attrs = x509_auth.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        alias = cn_attrs[0].value if cn_attrs else "Usuario DNIe"
        
        static_pub_bytes = x509_auth.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.UncompressedPoint
        )
        
        # Obtener ambos certs y claves privadas (re-open con PIN)
        token = obtener_token()
        with token.open(rw=True) as session:
            cert_objects = list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
            if len(cert_objects) < 2:
                raise RuntimeError("Se necesitan al menos 2 certificados (auth/sign).")
            auth_der = cert_objects[0][Attribute.VALUE]
            sign_der = cert_objects[1][Attribute.VALUE]
        
        # Obtener claves privadas con PIN
        with token.open(user_pin=pin) as session:
            priv_objects = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
            if len(priv_objects) < 2:
                raise RuntimeError("Se necesitan al menos 2 claves privadas (auth/sign).")
            static_priv = priv_objects[0]  # EC auth
            signing_priv = priv_objects[1]  # RSA sign
        
        print(f"DNIe cargado: {alias} (FP: {fingerprint(static_pub_bytes)[:8]})")
        return Identity(static_priv, static_pub_bytes, alias, auth_der, signing_priv, sign_der, pin, token)
    
    except Exception as e:
        raise ValueError(f"Error cargando DNIe: {e}")

def fingerprint(static_pub_bytes: bytes) -> str:
    return sha256(static_pub_bytes).hexdigest()

def sign_messages(identity: Identity, data: bytes) -> bytes:
    """Firma usando firmar_con_dni exacto"""
    return firmar_con_dni(identity.pin, data)

def verify_messages(identity: Identity, data: bytes, signature: bytes) -> bool:
    """Verifica firma con cert sign RSA"""
    try:
        x509_sign = x509.load_der_x509_certificate(identity.signing_cert_der)
        pk = x509_sign.public_key()
        pk.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False

# =====================================================================
# 2. CRIPTOGRAFÍA (ECDH + ChaCha20Poly1305)
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

def derive_session_keys(identity: Identity, our_eph, 
                       peer_static_pub: bytes, peer_eph_pub: bytes, is_initiator: bool) -> SessionKeys:
    """Deriva claves ECDH con static EC priv"""
    with identity.token.open(user_pin=identity.pin) as session:
        static_priv = identity.static_priv
        
        # ss: our_static x peer_static
        ss = static_priv.derive_key(
            session,
            Mechanism(Mechanism.ECDH, param=SECP256R1_OID),
            param=peer_static_pub
        )
        
        # se: our_static x peer_eph
        se = static_priv.derive_key(
            session,
            Mechanism(Mechanism.ECDH, param=SECP256R1_OID),
            param=peer_eph_pub
        )
        
        # es: our_eph x peer_static
        es = our_eph.derive_key(
            session,
            Mechanism(Mechanism.ECDH, param=SECP256R1_OID),
            param=peer_static_pub
        )
        
        # ee: our_eph x peer_eph
        ee = our_eph.derive_key(
            session,
            Mechanism(Mechanism.ECDH, param=SECP256R1_OID),
            param=peer_eph_pub
        )
        
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

HEADER = struct.Struct("!IIBBH")
CERT_HEADER = struct.Struct("!H")

def pack_frame(cid: int, stream_id: int, ftype: FrameType, payload: bytes) -> bytes:
    return HEADER.pack(cid, stream_id, ftype.value, 0, 0) + payload

def unpack_frame(data: bytes):
    cid, sid, ftype, _, _ = HEADER.unpack(data[:12])
    return cid, sid, FrameType(ftype), data[12:]

# =====================================================================
# 4. HANDSHAKE
# =====================================================================

def build_handshake(identity: Identity) -> Tuple[any, bytes]:
    """Genera eph EC keypair temp"""
    with identity.token.open(user_pin=identity.pin) as session:
        # Gen eph EC
        ec_params = {Attribute.EC_PARAMS: SECP256R1_OID}
        pub_attr = {
            Attribute.PRIVATE: False,
            Attribute.TOKEN: False,
            Attribute.CLASS: ObjectClass.PUBLIC_KEY,
        }
        priv_attr = {
            Attribute.PRIVATE: True,
            Attribute.SENSITIVE: True,
            Attribute.EXTRACTABLE: False,
            Attribute.TOKEN: False,
            Attribute.CLASS: ObjectClass.PRIVATE_KEY,
        }
        mech = Mechanism(Mechanism.EC_KEY_PAIR_GEN, SECP256R1_OID)
        pub, priv = session.generate_keypair(mech, pub_attr, priv_attr)
        
        eph_pub = pub[Attribute.EC_POINT]
        
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
    pending_eph: Optional[any] = None

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
            
            alias_key = hkdf_blake2s(peer_fp.encode(), b"alias-key", 32)
            peer_alias = decrypt(alias_key, nonce, enc_alias).decode("utf-8")
            
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
        except Exception:
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
            json_part, signature = data[:-256], data[-256:]
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
        except Exception:
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
# 10. MAIN
# =====================================================================

async def main():
    if not detectar_dnie():
        print("No se detectó lector de tarjetas o DNIe insertado.")
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
            if attempt < max_attempts:
                print("Intenta de nuevo...")
                attempt += 1
                continue
            else:
                print(f"Máximo de intentos ({max_attempts}) alcanzado. Saliendo.")
                sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(0)
    
    if not identity:
        print(f"\nError persistente. Saliendo.")
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
        transport.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"\n=== ERROR INESPERADO ===")
        print(f"Detalles: {e}")
        sys.exit(1)
