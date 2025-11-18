#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instant Messaging with DNIe Identity
Implementación completa según A2_IMP_intro.pdf con integración DNIe via PKCS#11
(Integrado exacto: Detección pyscard + Clase manejo_datos adaptada para EC/RSA + OpenSC)
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

# Verifica si la librería pyscard está instalada (exacto de working)
try:
    from smartcard.System import readers
except ImportError:
    print("La librería 'pyscard' no está instalada.")
    print("Para instalarla, ejecuta el siguiente comando en tu terminal:")
    print("pip install pyscard")
    sys.exit()

import pkcs11  # pkcs11.lib, etc. (exacto de working)
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

# DLL de working (OpenSC priorizado)
PKCS11_LIB = r"C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll"  # Exacto de working
SLOT_INDEX = 0  # Exacto de working

# OID para secp256r1 (ECDH auth)
SECP256R1_OID = b'\x06\x08\x2a\x86\x48\xce\x3d\x03\x01\x07'

# Códigos PKCS#11 para errores comunes
CKR_PIN_INCORRECT = 0xA0
CKR_PIN_LOCKED = 0xA4
CKR_TOKEN_NOT_PRESENT = 0x50
CKR_TOKEN_NOT_RECOGNIZED = 0x51
CKR_LIBRARY_FAILED_TO_LOAD = 0x20

# =====================================================================
# Detección DNIe con pyscard (exacto de working)
# =====================================================================

def detectar_dnie():
    """Función que detecta si un lector de tarjetas y un DNIe están conectados (exacto de working)."""
    try:
        # Obtener la lista de lectores de tarjetas disponibles
        lista_lectores = readers()
        
        # Si no se detecta ningún lector, muestra un mensaje y termina
        if not lista_lectores:
            return False

        #print(f" Lector de tarjetas detectado: {lista_lectores[0]}")
        #print("Esperando la inserción del DNIe...")
        
        # Obtener el primer lector disponible
        lector = lista_lectores[0]
        
        # Conectarse al lector
        conexion = lector.createConnection()
        
        try:
            # Intentar conectarse a la tarjeta (DNIe)
            conexion.connect()
            
            # Si la conexión es exitosa, se ha detectado un DNIe
            #print("¡DNIe detectado!")
            return True
            
        except Exception as e:
            # Si no se puede conectar, es probable que no haya un DNIe insertado
            #print("No se ha detectado el DNIe en el lector.")
            return False
            
    except Exception as e:
        #print(f"Se ha producido un error")
        return False

# =====================================================================
# Clase manejo_datos adaptada para IM (exacto de working + EC/RSA split)
# =====================================================================

class ManejoDNIeDatos:
    """Clase adaptada exacta de manejo_datos para IM: Verifica PIN, carga cert/claves, firma RSA, ECDH EC"""
    
    def __init__(self, pin: str):
        self.pin = pin  # Exacto
        self.token = self.obtener_token()  # Exacto
        self.cert = self.obtener_certificado_autenticacion()  # Auth EC
        self.serial_hash = self.obtener_hash_serial()  # No usado en IM, pero kept
        # Para IM: Cache de claves privadas
        self.static_priv = None  # EC auth para ECDH
        self.signing_priv = None  # RSA sign
        self.cargar_claves_privadas()  # Nuevo: Carga EC/RSA split
    
    # Exacto de working
    def obtener_token(self):
        pkcs11 = pkcs11.lib(PKCS11_LIB)
        slots = pkcs11.get_slots(token_present=True)
        if not slots:
            raise RuntimeError("No se encontró token DNIe.")
        return slots[self.SLOT_INDEX].get_token()
    
    # Exacto de working: Verifica PIN
    def verificar_dnie(self, pin):
        try:
            token = self.obtener_token()
            with token.open(user_pin=pin):  # Exacto: open con PIN verifica
                return True
        except Exception:  # Exacto
            return False 
    
    # Exacto de working: Cert auth (asumimos [0] es EC auth)
    def obtener_certificado_autenticacion(self):
        with self.token.open(rw=True) as session:  # Exacto: rw=True, no PIN para certs
            certificados = list(session.get_objects({pkcs11.Attribute.CLASS: pkcs11.ObjectClass.CERTIFICATE}))  # Exacto
            if not certificados: 
                raise RuntimeError("No se encontró certificado en el DNIe.")
            der = certificados[0][pkcs11.Attribute.VALUE]  # Exacto: [0]
            return x509.load_der_x509_certificate(der)  # Exacto

    # Exacto de working
    def obtener_hash_serial(self) -> str:
        serial = str(self.cert.serial_number).encode('utf-8')
        h = sha256(serial).hexdigest()[:16]
        return h

    # Nuevo: Carga claves privadas separadas (basado en working keys list, pero split EC/RSA)
    def cargar_claves_privadas(self):
        with self.token.open(user_pin=self.pin) as session:  # Necesita PIN para priv keys
            # Exacto de working para sign: list(session.get_objects(PRIVATE_KEY))
            keys = list(session.get_objects({pkcs11.Attribute.CLASS: pkcs11.ObjectClass.PRIVATE_KEY}))  # Exacto
            if not keys:
                raise RuntimeError("No se encontró clave privada en el token.")
            
            # Split: Busca EC para auth (estándar DNIe: primera o por type)
            ec_keys = [k for k in keys if k[pkcs11.Attribute.KEY_TYPE] == pkcs11.KeyType.EC]
            rsa_keys = [k for k in keys if k[pkcs11.Attribute.KEY_TYPE] == pkcs11.KeyType.RSA]
            
            if not ec_keys:
                raise RuntimeError("No se encontró clave EC (auth) en el DNIe.")
            self.static_priv = ec_keys[0]  # Primera EC como auth para ECDH
            
            if not rsa_keys:
                raise RuntimeError("No se encontró clave RSA (sign) en el DNIe.")
            self.signing_priv = rsa_keys[0]  # Primera RSA como sign (working usa [1], pero adaptamos a primera si solo una)

            print(f"Claves cargadas: EC auth ({len(ec_keys)}), RSA sign ({len(rsa_keys)})")  # Debug

    # Exacto de working para sign (adaptado a self.signing_priv)
    def firmar_con_dni(self, data: bytes) -> bytes:
        with self.token.open(user_pin=self.pin) as session:
            # En working: keys = list(...), priv = keys[1]
            # Aquí: Usa self.signing_priv precargada
            try:
                # Exacto: Mechanism.SHA256_RSA_PKCS (pero en py-pkcs11, es RSA_PKCS con hash param)
                mech = pkcs11.Mechanism(pkcs11.Mechanism.RSA_PKCS, hashes.SHA256())  # Adaptado estandar
                signature = self.signing_priv.sign(data, mech)
                return signature
            except Exception as e:
                raise RuntimeError(
                    "El DNIe no pudo firmar con SHA256_RSA_PKCS. "
                    f"Asegúrate de drivers/DNIe compatibles. Error: {e}"
                ) from e

# =====================================================================
# 1. IDENTIDAD con DNIe (usando ManejoDNIeDatos exacto)
# =====================================================================

@dataclass
class Identity:
    static_priv: pkcs11.Object  # Clave EC auth del DNIe (derive_key)
    static_pub_bytes: bytes
    alias: str
    cert_der: bytes  # Cert auth EC DER
    signing_priv: pkcs11.Object  # Clave RSA sign del DNIe
    signing_cert_der: bytes  # Cert sign RSA DER (nuevo cert para sign)
    token: pkcs11.Token  # Token para open
    cert_auth: x509.Certificate  # De working
    serial_hash: str  # De working (opcional)
    pin: str  # Para re-open si needed

def load_identity_dnie(pin: str) -> Identity:
    """Carga usando ManejoDNIeDatos exacto; verifica PIN primero, luego carga full"""
    try:
        # Paso 1: Verificar PIN exacto de working
        md = ManejoDNIeDatos("")  # Temp sin pin
        if not md.verificar_dnie(pin):
            raise ValueError("PIN incorrecto o DNIe no válido.")
        
        # Paso 2: Cargar full con PIN (exacto __init__)
        md = ManejoDNIeDatos(pin)
        
        # Obtener cert sign RSA (similar a auth, pero busca RSA cert)
        with md.token.open(rw=True) as session:
            certs = list(session.get_objects({pkcs11.Attribute.CLASS: pkcs11.ObjectClass.CERTIFICATE}))
            if len(certs) < 2:
                raise RuntimeError("No suficientes certs para auth/sign.")
            # Asumir [0] auth EC, [1] sign RSA (estándar DNIe)
            auth_der = certs[0][pkcs11.Attribute.VALUE]
            sign_der = certs[1][pkcs11.Attribute.VALUE]  # Nuevo: Cert sign
        
        x509_auth = md.cert  # Exacto de working
        cn_attrs = x509_auth.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        alias = cn_attrs[0].value if cn_attrs else x509_auth.subject.rfc4514_string()[:20] or "Usuario DNIe"
        
        static_pub_bytes = x509_auth.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.UncompressedPoint
        )
        
        print(f"DNIe cargado exitosamente: {alias} (FP EC: {fingerprint(static_pub_bytes)[:8]})")
        
        return Identity(
            md.static_priv, static_pub_bytes, alias, auth_der,
            md.signing_priv, sign_der, md.token, x509_auth, md.serial_hash, pin
        )
    
    except RuntimeError as re:
        raise ValueError(f"Error en DNIe (de working): {re}")
    except OSError:
        raise ValueError(f"DLL OpenSC no encontrada: {PKCS11_LIB}. Instala OpenSC desde https://opencsc-project.org/")
    except Exception as e:
        raise ValueError(f"Error cargando DNIe: {e}. Verifica drivers/OpenSC.")

def fingerprint(static_pub_bytes: bytes) -> str:
    return sha256(static_pub_bytes).hexdigest()

def sign_messages(identity: Identity, data: bytes) -> bytes:
    """Firma exacta de working con RSA sign"""
    md_temp = ManejoDNIeDatos(identity.pin)  # Re-crea para sign (o cache, pero simple)
    md_temp.signing_priv = identity.signing_priv  # Set manual
    md_temp.token = identity.token
    return md_temp.firmar_con_dni(data)

def verify_messages(identity: Identity, data: bytes, signature: bytes) -> bool:
    """Verifica con cert sign RSA (PKCS1v15 SHA256)"""
    try:
        x509_sign = x509.load_der_x509_certificate(identity.signing_cert_der)
        pk = x509_sign.public_key()
        pk.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False

# =====================================================================
# 2. CRIPTOGRAFÍA (ECDH con auth EC + Firma RSA)
# =====================================================================

@dataclass
class SessionKeys:
    send_key: bytes
    recv_key: bytes

def hkdf_blake2s(ikm: bytes, info: bytes, length: int = 64) -> bytes:
    return HKDF(algorithm=hashes.BLAKE2s(32), length=length, salt=None, info=info).derive(ikm)

def derive_session_keys(identity: Identity, our_eph: pkcs11.Object, 
                       peer_static_pub: bytes, peer_eph_pub: bytes, is_initiator: bool) -> SessionKeys:
    """Deriva con EC auth priv (derive_key)"""
    # Re-open session para derive (PKCS11 stateful)
    with identity.token.open(user_pin=identity.pin) as session:
        # ss: static x peer_static
        ss_mech = pkcs11.Mechanism(pkcs11.MechanismType.ECDH, param=SECP256R1_OID)
        identity.static_priv[session]  # Bind to session if needed
        ss = identity.static_priv.derive_key(session, ss_mech, param=peer_static_pub)
        
        # se: static x peer_eph
        se = identity.static_priv.derive_key(session, ss_mech, param=peer_eph_pub)
        
        # es: eph x peer_static
        es = our_eph.derive_key(session, ss_mech, param=peer_static_pub)
        
        # ee: eph x peer_eph
        ee = our_eph.derive_key(session, ss_mech, param=peer_eph_pub)
        
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
CERT_HEADER = struct.Struct("!H")  # Longitud cert DER

def pack_frame(cid: int, stream_id: int, ftype: FrameType, payload: bytes) -> bytes:
    return HEADER.pack(cid, stream_id, ftype.value, 0, 0) + payload

def unpack_frame(data: bytes):
    cid, sid, ftype, _, _ = HEADER.unpack(data[:12])
    return cid, sid, FrameType(ftype), data[12:]

# =====================================================================
# 4. HANDSHAKE (genera eph EC temp)
# =====================================================================

def build_handshake(identity: Identity) -> Tuple[pkcs11.Object, bytes]:
    """Eph temp EC en session, cert auth, nonce, enc_alias"""
    with identity.token.open(user_pin=identity.pin) as session:
        # Gen eph EC
        ec_params = {pkcs11.Attribute.EC_PARAMS: SECP256R1_OID}
        # Nota: Gen keypair puede variar; adaptado genérico (si no soporta domain, usa gen mechanism)
        try:
            domain = session.create_domain_parameters(pkcs11.KeyType.EC, ec_params)
            pub_attr = {pkcs11.Attribute.PRIVATE: False, pkcs11.Attribute.TOKEN: False}
            priv_attr = {pkcs11.Attribute.PRIVATE: True, pkcs11.Attribute.SENSITIVE: True, pkcs11.Attribute.TOKEN: False}
            pub, priv = domain.generate_keypair(pub_attr, priv_attr)
        except:
            # Fallback gen: Mechanism EC_KEY_PAIR_GEN
            mech = pkcs11.Mechanism(pkcs11.MechanismType.EC_KEY_PAIR_GEN, ec_params)
            pub_attr = {pkcs11.Attribute.PRIVATE: False, pkcs11.Attribute.TOKEN: False}
            priv_attr = {pkcs11.Attribute.PRIVATE: True, pkcs11.Attribute.SENSITIVE: True, pkcs11.Attribute.TOKEN: False}
            pub, priv = session.generate_keypair(mech, pub_attr, priv_attr)
        
        eph_pub = pub[pkcs11.Attribute.EC_POINT]
        
        fp = fingerprint(identity.static_pub_bytes)
        alias_key = hkdf_blake2s(fp.encode(), b"alias-key", 32)
        nonce, encrypted = encrypt(alias_key, identity.alias.encode("utf-8"))
        
        cert_len = CERT_HEADER.pack(len(identity.cert_der))
        payload = eph_pub + cert_len + identity.cert_der + nonce + encrypted
        return priv, payload

def parse_handshake(data: bytes):
    eph_pub_len = 65
    eph_pub = data[:eph_pub_len]
    cert_len = CERT_HEADER.unpack(data[eph_pub_len:eph_pub_len+2])[0]
    cert_start = eph_pub_len + 2
    cert_end = cert_start + cert_len
    cert_der = data[cert_start:cert_end]
    nonce = data[cert_end:cert_end+12]
    enc_alias = data[cert_end+12:]
    
    x509_cert = x509.load_der_x509_certificate(cert_der)
    if not isinstance(x509_cert.public_key(), ec.EllipticCurvePublicKey):
        raise ValueError("Cert no EC.")
    static_pub = x509_cert.public_key().public_bytes(Encoding.Raw, PublicFormat.UncompressedPoint)
    return eph_pub, static_pub, nonce, enc_alias, cert_der

# =====================================================================
# 5-9. Contactos, Session, UdpNode, Mdns, TUI (sin cambios mayores, adapt sign size a 256)
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
    pending_eph: Optional[pkcs11.Object] = None

# UdpNode, MdnsService, ChatTUI (copiados de prev, con sig size 256 en load_messages)
class UdpNode(asyncio.DatagramProtocol):
    # ... (exacto de prev, sin cambios)
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

# MdnsService (exacto de prev)
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
    # ... (exacto de prev, con load_messages ajust sig=256)
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
            # RSA 1024 sig ~128, pero pad a 256 como working
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
            data_dict = {fp: [asdict(m) for m in msgs] for fp, msgs in self.chat_history.items()}
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
# 10. MAIN (detección + verify PIN primero, luego load)
# =====================================================================

async def main():
    # Detección exacta de working
    if not detectar_dnie():
        print("No se detectó lector o DNIe. Verifica hardware.")
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
                print("Máximo intentos alcanzado. Saliendo.")
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
