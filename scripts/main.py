# main.py
import asyncio
import sys
import os
import config
from getpass import getpass

# Importaciones para sacar el nombre del certificado
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import NameOID

from dnie_manager import DNIeManager
from protocol import SecureIMProtocol
from discovery import DiscoveryService
from gui import ChatGUI

async def main():
    # 1. Configuración Puerto
    port = config.UDP_PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    
    print(f"--- DNIe CHAT (Puerto {port}) ---")
    
    # 2. Login DNIe y Extracción de Identidad
    try:
        pin = getpass("Introduce PIN DNIe: ")
        print("⌛ Leyendo tarjeta...")
        dnie = DNIeManager(pin)
        
        # --- LÓGICA RECUPERADA: EXTRAER NOMBRE DEL DNIe ---
        # No pedimos nickname, lo sacamos del certificado directamente
        cert = x509.load_der_x509_certificate(dnie.cert_der, default_backend())
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if cn_attrs:
            raw_name = cn_attrs[0].value
            # Limpiamos la basura que mete el DNIe
            my_nick = raw_name.replace("(Autenticación)", "").replace("(Firma)", "").strip()
        else:
            my_nick = "DNIe Desconocido"
            
        print(f"✅ Identidad cargada: {my_nick}")
        # --------------------------------------------------

    except Exception as e:
        print(f"❌ Error DNIe: {e}")
        # Si falla el DNIe, salimos o usamos un dummy para probar
        # return 
        my_nick = "Usuario Sin DNIe" # Fallback por si estás probando sin tarjeta

    # DB Dummy (Si no la usas, pasamos None)
    db = None 

    # 3. Iniciar Sistema
    loop = asyncio.get_running_loop()
    
    # Callback para recibir mensajes
    def protocol_callback(addr, text, nombre):
        gui.on_protocol_msg(addr, text, nombre)

    # Protocolo
    protocol = SecureIMProtocol(dnie, db, protocol_callback)
    
    # GUI (Usamos el nombre del DNIe como nick)
    gui = ChatGUI(protocol, my_nick, db)
    
    # Red UDP
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol, local_addr=('0.0.0.0', port)
    )
    protocol.transport = transport 
    
    # Discovery (mDNS)
    def discovery_callback(name, ip, p):
        gui.add_peer(name, ip, p)
        
    mdns = DiscoveryService(port, my_nick, discovery_callback)
    await mdns.start()

    # 4. Correr todo
    try:
        await gui.run()
    finally:
        await mdns.stop()

if __name__ == "__main__":
    # Fix para Windows (necesario para la GUI)
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error fatal: {e}")