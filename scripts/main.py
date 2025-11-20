# main.py
import asyncio
import argparse # Lo mantenemos por si acaso, pero no lo usaremos directamente
import os
import signal
from prompt_toolkit.patch_stdout import patch_stdout
from dnie_manager import DNIeManager
from discovery import DiscoveryService
from protocol import SecureIMProtocol
from gui import ChatGUI # Cambiado: from scripts.gui import ChatGUI -> from gui import ChatGUI
from database import DatabaseManager # Cambiado: from db import DatabaseManager -> from database import DatabaseManager

# --- Configuración fija ---
DEFAULT_PORT = 6666 # El puerto siempre es 6666

async def main():
    print("Iniciando DNIe Secure IM...")

    # --- Inicializar DNIe Manager ---
    dnie_manager = DNIeManager()
    if not dnie_manager.private_key:
        print("ERROR: No se pudo cargar el DNIe o acceder a la clave privada.")
        return
    
    # --- Obtener Nick del DNIe ---
    # Asumiendo que dnie_manager tiene un método para obtener el CN (Common Name)
    # o que ya lo extrae al inicializar
    # Adaptar según cómo tu DNIeManager extrae el nombre
    my_nick = dnie_manager.get_cn() # Suponiendo que tienes este método en dnie_manager
    if not my_nick:
        print("ERROR: No se pudo obtener el nombre de usuario (CN) del DNIe.")
        return

    print(f"Usuario DNIe: {my_nick}, Puerto: {DEFAULT_PORT}")

    # --- Inicializar Database Manager ---
    db = DatabaseManager(f"db_{my_nick}.json")

    # --- Inicializar GUI ---
    gui = ChatGUI(protocol=None, my_nick=my_nick, db=db)
    
    # --- Inicializar Protocolo ---
    loop = asyncio.get_running_loop()
    transport, protocol_instance = await loop.create_datagram_endpoint(
        lambda: SecureIMProtocol(dnie_manager, gui.on_protocol_msg),
        local_addr=('0.0.0.0', DEFAULT_PORT)
    )
    gui.protocol = protocol_instance # Asignar la instancia del protocolo a la GUI

    # --- Inicializar Discovery ---
    def on_peer_found(name, ip, port):
        gui.add_or_update_peer(name, ip, port, from_zeroconf=True) 
    
    discovery_service = DiscoveryService(DEFAULT_PORT, my_nick, on_peer_found)
    await discovery_service.start()

    # --- Arrancar GUI y manejar interrupciones ---
    with patch_stdout():
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        if os.name == 'nt': 
            pass 
        else: 
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)

        try:
            await asyncio.gather(
                gui.run(),
                stop_event.wait()
            )
        except Exception as e:
            print(f"Error en la aplicación principal: {e}")
        finally:
            print("\nCerrando servicios...")
            await discovery_service.stop()
            transport.close()
            if not gui.app.is_exited:
                gui.app.exit()


if __name__ == "__main__":
    asyncio.run(main())