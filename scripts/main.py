# main.py
import asyncio
import sys
import config
import pkcs11 
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
        # 1. Pedir PIN de forma oculta
        pin = getpass("Introduce PIN DNIe: ")
        print("⌛ Accediendo a DNIe...")
        
        # 2. Inicializar Manager con PIN
        dnie = DNIeManager(pin)
        
        # 3. Generar ID único firmado por el DNIe (para nombre de archivo BD)
        print("⌛ Generando ID único seguro para la Base de Datos...")
        db_id = dnie.get_unique_id()
        
        # 4. Obtener Nick
        print("⌛ Obteniendo nombre del usuario...")
        nick = dnie.get_user_name()
        print(f"✅ Bienvenido: {nick}")
        
        # 5. Inicializar BD Cifrada (Deriva clave usando firma DNIe)
        db = JsonEncryptedDB(dnie, db_id)

    except pkcs11.exceptions.PinIncorrect:
        print("\n❌ Error: El PIN introducido es incorrecto.")
        return
    except pkcs11.exceptions.CardNotPresent:
        print("\n❌ Error: No se detecta el DNIe. Insértalo correctamente.")
        return
    except Exception as e:
        print(f"\n❌ Error Crítico: {e}")
        # import traceback; traceback.print_exc() # Descomentar para ver detalles
        return

    loop = asyncio.get_running_loop()
    
    # Callbacks para la GUI
    def protocol_cb(addr, text, nombre):
        gui.on_protocol_msg(addr, text, nombre)

    def discovery_cb(name, ip, p):
        gui.add_or_update_peer(name, ip, p)

    # Inicializar componentes
    protocol = SecureIMProtocol(dnie, protocol_cb)
    gui = ChatGUI(protocol, nick, db)
    mdns = DiscoveryService(port, nick, discovery_cb)

    # Arrancar servidor UDP
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol, local_addr=('0.0.0.0', port)
    )
    
    # Arrancar Discovery y GUI
    await mdns.start()
    try:
        await gui.run()
    finally:
        await mdns.stop()
        transport.close()

if __name__ == "__main__":
    # Fix para Windows y selectores asíncronos
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass