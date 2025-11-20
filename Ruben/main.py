# main.py
import asyncio
import sys
import config
import pkcs11 # Necesario para capturar el error de PIN
from getpass import getpass
from dnie_manager import DNIeManager
from protocol import SecureIMProtocol
from discovery import DiscoveryService
from gui import ChatGUI
from database import JsonEncryptedDB

async def main():
    port = config.UDP_PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    
    print(f"--- DNIe CHAT (Puerto {port}) ---")
    
    try:
        pin = getpass("Introduce PIN DNIe: ")
        print("⌛ Accediendo a DNIe...")
        dnie = DNIeManager(pin)
        
        print("⌛ Generando ID único para la Base de Datos...")
        db_id = dnie.get_unique_id()
        
        print("⌛ Obteniendo nombre del usuario...")
        nick = dnie.get_user_name()
        print(f"✅ Bienvenido: {nick}")
        
        # Inicializar BD JSON Cifrada
        db = JsonEncryptedDB(dnie, db_id)

    except pkcs11.exceptions.PinIncorrect:
        print("\n❌ Error: El PIN introducido es incorrecto.")
        return
    except pkcs11.exceptions.CardNotPresent:
        print("\n❌ Error: No se detecta el DNIe. Insértalo correctamente.")
        return
    except Exception as e:
        print(f"\n❌ Error Crítico: {e}")
        # import traceback; traceback.print_exc() # Descomentar para depurar
        return

    loop = asyncio.get_running_loop()
    
    def protocol_cb(addr, text, nombre):
        gui.on_protocol_msg(addr, text, nombre)

    def discovery_cb(name, ip, p):
        gui.add_or_update_peer(name, ip, p)

    protocol = SecureIMProtocol(dnie, protocol_cb)
    gui = ChatGUI(protocol, nick, db)
    mdns = DiscoveryService(port, nick, discovery_cb)

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