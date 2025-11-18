import asyncio
import sys
import os
import config
from getpass import getpass
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
    
    # 2. Login DNIe (CLI simple al principio)
    try:
        pin = getpass("Introduce PIN DNIe: ")
        print("⌛ Leyendo tarjeta...")
        dnie = DNIeManager(pin)
        print(f"✅ Identidad cargada. Certificado de: {len(dnie.cert_der)} bytes")
    except Exception as e:
        print(f"❌ Error DNIe: {e}")
        return

    nick = input("Elige tu Nickname para la red: ").strip() or "Usuario"

    # 3. Iniciar Sistema
    loop = asyncio.get_running_loop()
    
    # Creamos GUI primero para pasarle el callback
    # Pero la GUI necesita el protocolo... Huevo y gallina.
    # Solución: Creamos GUI vacía y luego le inyectamos el protocolo.
    
    # Paso A: Protocolo (necesita callback para cuando llegan mensajes)
    # Usaremos un wrapper para conectar protocolo -> GUI
    def protocol_callback(addr, text, nombre):
        gui.on_protocol_msg(addr, text, nombre)

    protocol = SecureIMProtocol(dnie, protocol_callback)
    
    # Paso B: GUI
    gui = ChatGUI(protocol, nick)
    
    # Paso C: Red UDP
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol, local_addr=('0.0.0.0', port)
    )
    
    # Paso D: Discovery (necesita callback para cuando encuentra gente)
    def discovery_callback(name, ip, p):
        gui.add_peer(name, ip, p)
        
    mdns = DiscoveryService(port, nick, discovery_callback)
    await mdns.start()

    # 4. Correr GUI
    try:
        await gui.run()
    finally:
        await mdns.stop()
        transport.close()

if __name__ == "__main__":
    # Fix para Windows y Prompt Toolkit + Asyncio
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Lanzador simple en ventana nueva si no tiene argumentos
    # (Opcional, puedes ejecutarlo manual: python main.py 6666)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass