# main.py
import asyncio
import sys
import config
from getpass import getpass
from dnie_manager import DNIeManager
from protocol import SecureIMProtocol
from discovery import DiscoveryService
from gui import ChatGUI

async def main():
    # 1. Puerto por argumento (para poder abrir varias ventanas)
    port = config.UDP_PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    
    print(f"--- DNIe CHAT (Puerto {port}) ---")
    
    # 2. Login
    try:
        pin = getpass("Introduce PIN DNIe: ")
        print("⌛ Leyendo tarjeta...")
        dnie = DNIeManager(pin)
        print(f"✅ Identidad cargada. Cert: {len(dnie.cert_der)} bytes")
    except Exception as e:
        print(f"❌ Error DNIe: {e}")
        return

    nick = input("Elige tu Nick: ").strip() or "Usuario"

    # 3. Inicialización
    loop = asyncio.get_running_loop()
    
    # Callback: Protocolo -> GUI
    def protocol_cb(addr, text, nombre):
        gui.on_protocol_msg(addr, text, nombre)

    protocol = SecureIMProtocol(dnie, protocol_cb)
    gui = ChatGUI(protocol, nick)
    
    # Callback: Discovery -> GUI
    def discovery_cb(name, ip, p):
        gui.add_peer(name, ip, p)

    mdns = DiscoveryService(port, nick, discovery_cb)

    # 4. Arrancar
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol, local_addr=('0.0.0.0', port)
    )
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