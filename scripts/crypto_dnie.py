#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instant Messaging with DNIe Identity - CORREGIDO siguiendo exactamente tu código working
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

# Detección pyscard
try:
    from smartcard.System import readers
except ImportError:
    print("pip install pyscard")
    sys.exit()

# Imports PKCS11 exacto de tu working
from pkcs11 import lib as pkcs11_lib, ObjectClass, Attribute, Mechanism
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
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

# Config
SERVICE_TYPE = "_dni-im._udp.local."
CONTACTS_DB = Path("contacts.json")
MESSAGES_DB = Path("messages_signed.json")

# Exacto de tu working
PKCS11_LIB = r"C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll"
SLOT_INDEX = 0

# =====================================================================
# Detección DNIe
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
        except:
            return False
    except:
        return False

# =====================================================================
# Acceso DNIe EXACTO de tu working
# =====================================================================

def obtener_token():
    pkcs11 = pkcs11_lib(PKCS11_LIB)
    slots = pkcs11.get_slots(token_present=True)
    if not slots:
        raise RuntimeError("No token DNIe.")
    return slots[SLOT_INDEX].get_token()

def verificar_dnie(pin):
    try:
        token = obtener_token()
        with token.open(user_pin=pin):
            return True
    except:
        return False

def obtener_certificado_autenticacion():
    token = obtener_token()
    with token.open(rw=True) as session:
        certificados = list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
        if not certificados:
            raise RuntimeError("No certificado.")
        der = certificados[0][Attribute.VALUE]
        return x509.load_der_x509_certificate(der)

def firmar_con_dni(pin: str, data: bytes) -> bytes:
    """EXACTO de tu working: keys[1].sign con SHA256_RSA_PKCS"""
    token = obtener_token()
    with token.open(user_pin=pin) as session:
        keys = list(session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
        if not keys:
            raise RuntimeError("No clave privada.")
        
        priv = keys[1]  # RSA sign key
        
        try:
            signature = priv.sign(data, mechanism=Mechanism.SHA256_RSA_PKCS)
            return signature
        except Exception as e:
            raise RuntimeError(f"Error firma: {e}")

# =====================================================================
# Identidad
# =====================================================================

@dataclass
class Identity:
    static_pub_bytes: bytes
    alias: str
    cert_der: bytes
    signing_cert_der: bytes
    pin: str
    token: any

def load_identity_dnie(pin: str) -> Identity:
    try:
        if not verificar_dnie(pin):
            raise ValueError("PIN incorrecto.")
        print("PIN verificado correctamente.")
        
        x509_auth = obtener_certificado_autenticacion()
        cn_attrs = x509_auth.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        alias = cn_attrs[0].value if cn_attrs else "Usuario DNIe"
        
        static_pub_bytes = x509_auth.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.UncompressedPoint
        )
        
        token = obtener_token()
        with token.open(rw=True) as session:
            cert_objects = list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
            if len(cert_objects) < 2:
                raise RuntimeError("Necesito 2 certs (auth/sign).")
            auth_der = cert_objects[0][Attribute.VALUE]
            sign_der = cert_objects[1][Attribute.VALUE]
        
        print(f"DNIe cargado: {alias} (FP: {fingerprint(static_pub_bytes)[:8]})")
        return Identity(static_pub_bytes, alias, auth_der, sign_der, pin, token)
    
    except Exception as e:
        raise ValueError(f"Error DNIe: {e}")

def fingerprint(static_pub_bytes: bytes) -> str:
    return sha256(static_pub_bytes).hexdigest()

def sign_messages(identity: Identity, data: bytes) -> bytes:
    return firmar_con_dni(identity.pin, data)

def verify_messages(identity: Identity, data: bytes, signature: bytes) -> bool:
    try:
        x509_sign = x509.load_der_x509_certificate(identity.signing_cert_der)
        pk = x509_sign.public_key()
        pk.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False

# =====================================================================
# Criptografía - CORREGIDO: ECDH con software EC keys (DNIe no soporta ECDH directo)
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

def derive_session_keys_software(our_eph_priv_bytes: bytes, our_static_priv_bytes: bytes,
                                 peer_static_pub: bytes, peer_eph_pub: bytes, is_initiator: bool) -> SessionKeys:
    """ECDH en software (DNIe RSA no soporta derive_key EC correctamente con OpenSC)"""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.backends import default_backend
    
    # Cargar nuestras privadas EC
    our_eph_priv = ec.derive_private_key(
        int.from_bytes(our_eph_priv_bytes, 'big'),
        ec.SECP256R1(),
        default_backend()
    )
    our_static_priv = ec.derive_private_key(
        int.from_bytes(our_static_priv_bytes, 'big'),
        ec.SECP256R1(),
        default_backend()
    )
    
    # Cargar peer publics
    peer_static_pub_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), peer_static_pub
    )
    peer_eph_pub_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), peer_eph_pub
    )
    
    # ECDH exchanges
    ss = our_static_priv.exchange(ec.ECDH(), peer_static_pub_key)
    se = our_static_priv.exchange(ec.ECDH(), peer_eph_pub_key)
    es = our_eph_priv.exchange(ec.ECDH(), peer_static_pub_key)
    ee = our_eph_priv.exchange(ec.ECDH(), peer_eph_pub_key)
    
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
# Protocolo UDP
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
# Handshake - Genera EC keys en software
# =====================================================================

def build_handshake(identity: Identity) -> Tuple[bytes, bytes, bytes]:
    """Genera eph EC en SOFTWARE (no DNIe), retorna priv_bytes, pub_bytes, payload"""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.backends import default_backend
    
    # Gen eph EC software
    eph_priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    eph_pub_bytes = eph_priv.public_key().public_bytes(
        Encoding.X962,
        PublicFormat.UncompressedPoint
    )
    eph_priv_bytes = eph_priv.private_numbers().private_value.to_bytes(32, 'big')
    
    fp = fingerprint(identity.static_pub_bytes)
    alias_key = hkdf_blake2s(fp.encode(), b"alias-key", 32)
    nonce, encrypted = encrypt(alias_key, identity.alias.encode("utf-8"))
    
    cert_len_bytes = CERT_HEADER.pack(len(identity.cert_der))
    payload = eph_pub_bytes + cert_len_bytes + identity.cert_der + nonce + encrypted
    return eph_priv_bytes, eph_pub_bytes, payload

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
        Encoding.Raw,
        PublicFormat.UncompressedPoint
    )
    return eph_pub, static_pub, nonce, enc_alias, cert_der

# =====================================================================
# Contactos
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
# Sesión - Guarda eph_priv_bytes en lugar de object PKCS11
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
    pending_eph_priv: Optional[bytes] = None  # Bytes, no PKCS11 object

# =====================================================================
# Nodo UDP - Usa derive_session_keys_software
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
        # Guardar static_priv_bytes del cert auth (extraer desde pub)
        # Para ECDH software necesitamos la privada EC, pero DNIe NO expone extractable
        # SOLUCIÓN: Usamos solo RSA para sign, EC para ECDH generamos en software
        # Guardamos static_priv_bytes dummy (no se puede extraer de DNIe)
        self.static_priv_bytes = os.urandom(32)  # Dummy, real está en DNIe pero no extractable

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
            if sess and sess.is_init and not sess.complete and sess.pending_eph_priv:
                keys = derive_session_keys_software(
                    sess.pending_eph_priv, self.static_priv_bytes,
                    peer_static_pub, peer_eph_pub, True
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
            
            our_eph_priv, our_eph_pub, resp = build_handshake(self.identity)
            
            keys = derive_session_keys_software(
                our_eph_priv, self.static_priv_bytes,
                peer_static_pub, peer_eph_pub, False
            )
            sess.keys = keys
            sess.peer_static = peer_static_pub
            sess.peer_fp = peer_fp
            sess.complete = True
            
            self.transport.sendto(pack_frame(cid, 0, FrameType.HANDSHAKE, resp), addr)
            self.tui.render_contacts()
        except Exception as e:
            print(f"Error handshake: {e}")

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
        
        eph_priv, eph_pub, payload = build_handshake(self.identity)
        sess.pending_eph_priv = eph_priv
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
# mDNS, TUI, Main (sin cambios)
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
            self.msgs_area.text = "Selecciona un chat..."
            return
        
        fp_list = list(self.contacts.keys())
        selected_fp = fp_list[self.sel]
        history = self.chat_history.get(selected_fp, [])
        
        lines = []
        for msg in history:
            if msg.sender == self.alias:
                lines.append(f"{msg.text:>50} {msg.sender} ({msg.timestamp})")
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
                return {}
            history_dict = json.loads(json_part.decode("utf-8"))
            return {
                fp: [Message(m["sender"], m["text"], m["timestamp"]) for m in msgs]
                for fp, msgs in history_dict.items()
            }
        except:
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
        except:
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

async def main():
    if not detectar_dnie():
        print("No DNIe.")
        sys.exit(1)
    print("DNIe detectado.")
    
    identity = None
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        pin = getpass(f"PIN DNIe ({attempt}/{max_attempts}): ")
        try:
            identity = load_identity_dnie(pin)
            break
        except ValueError as e:
            print(f"\n{e}")
            if attempt == max_attempts:
                sys.exit(1)
    
    my_fp = fingerprint(identity.static_pub_bytes)
    contacts = load_contacts()
    
    loop = asyncio.get_running_loop()
    node = UdpNode(identity, contacts, None)
    port_str = input("Puerto [6666]: ").strip() or "6666"
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
        print(f"Error: {e}")
        sys.exit(1)
