import asyncio
import sys
import config
from getpass import getpass

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import NameOID

from dnie_manager import DNIeManager
from protocol import SecureIMProtocol
from discovery import DiscoveryService
from gui import ChatGUI
from database import JsonDatabase

async def main():
    port = config.UDP_PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    
    print(f"--- DNIe CHAT (Puerto {port}) ---")
    
    loop = asyncio.get_running_loop()
    dnie = None
    db = None
    try:
        pin = getpass("Introduce PIN DNIe: ")
        print("⌛ Leyendo tarjeta...")
        dnie = DNIeManager(pin)
        
        db = JsonDatabase(dnie)
        
        cert = x509.load_der_x509_certificate(dnie.cert_der, default_backend())
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if cn_attrs:
            raw = cn_attrs[0].value
            my_nick = raw.replace("(AUTENTICACIÓN)", "").replace("(Autenticación)", "").replace("(FIRMA)", "").strip()
        else:
            my_nick = "Usuario Desconocido"

    except Exception as e:
        print(f"Error al leer DNIe: {e}")
        sys.exit(1)

    def protocol_callback(addr, text, nombre, msg_id=None):
        gui.on_protocol_msg(addr, text, nombre, msg_id)

    protocol = SecureIMProtocol(dnie, db, protocol_callback)
    
    # Obtener IP local antes de crear la GUI
    mdns_temp = DiscoveryService(port, my_nick, lambda n, i, p: None)
    my_ip = mdns_temp.get_lan_ip()
    
    gui = ChatGUI(protocol, my_nick, db, my_ip, port)
    
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol, local_addr=('0.0.0.0', port)
    )
    protocol.transport = transport 
    
    def discovery_callback(name, ip, p):
        contact_id = f"{ip}:{p}"
        existing = db.get_contact_info(contact_id)
        if existing:
            if existing.get("name") == name:
                return
        gui.add_peer(name, ip, p)
        
    mdns = DiscoveryService(port, my_nick, discovery_callback)
    await mdns.start()

    try:
        await gui.run()
    finally:
        await mdns.stop()
 
if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
