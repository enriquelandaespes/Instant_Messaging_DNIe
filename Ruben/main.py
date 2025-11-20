# main.py
import asyncio
import sys
import config
from getpass import getpass
from dnie_manager import DNIeManager
from protocol import SecureIMProtocol
from discovery import DiscoveryService
from gui import ChatGUI
from database import EncryptedDB

async def main():
    port = config.UDP_PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    
    print(f"--- DNIe CHAT (Puerto {port}) ---")
    
    try:
        # El PIN se usa para:
        # 1. Autenticar el Handshake
        # 2. Generar la clave para descifrar la Base de Datos local
        pin = getpass("Introduce PIN DNIe: ")
        print("⌛ Accediendo a DNIe...")
        dnie = DNIeManager(pin)
        
        print("⌛ Obteniendo nombre del usuario...")
        nick = dnie.get_user_name()
        print(f"✅ Bienvenido: {nick}")

        # Inicializar Base de Datos Cifrada
        db = EncryptedDB(dnie, nick)

    except Exception as e:
        print(f"❌ Error Crítico: {e}")
        return

    loop = asyncio.get_running_loop()
    
    # Callbacks
    def protocol_cb(addr, text, nombre):
        gui.on_protocol_msg(addr, text, nombre)

    def discovery_cb(name, ip, p):
        gui.add_or_update_peer(name, ip, p)

    protocol = SecureIMProtocol(dnie, protocol_cb)
    # Pasamos la BD a la GUI para que gestione el historial
    gui = ChatGUI(protocol, nick, db) 
    mdns = DiscoveryService(port, nick, discovery_cb)

    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol, local_addr=('0.0.0.0', port)
    )
    
    # Iniciar servicios
    await mdns.start()
    try:
        await gui.run()
    finally:
        await mdns.stop()
        transport.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass