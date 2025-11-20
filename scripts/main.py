# main.py
import asyncio
import argparse
import os
import signal
from prompt_toolkit.patch_stdout import patch_stdout
from dnie_manager import DNIeManager
from discovery import DiscoveryService
from protocol import SecureIMProtocol
from gui import ChatGUI
from db import DatabaseManager

# --- Configuración inicial ---
# Obtener un puerto aleatorio si no se especifica
DEFAULT_PORT = 6666 # Puedes cambiar esto si quieres un puerto fijo por defecto

async def main():
    parser = argparse.ArgumentParser(description="DNIe Secure IM Chat")
    parser.add_argument("--nick", type=str, required=True, help="Nickname for the chat")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="UDP Port for communication")
    args = parser.parse_args()

    print(f"Iniciando DNIe Secure IM con Nick: {args.nick}, Puerto: {args.port}")

    # --- Inicializar DNIe Manager ---
    dnie_manager = DNIeManager()
    if not dnie_manager.private_key:
        print("ERROR: No se pudo cargar el DNIe o acceder a la clave privada.")
        return

    # --- Inicializar Database Manager ---
    db = DatabaseManager(f"db_{args.nick}.json")

    # --- Inicializar GUI ---
    # La GUI necesita el protocolo para enviar mensajes/handshakes, pero el protocolo necesita la GUI para callbacks.
    # Inicializamos la GUI primero con un placeholder para el protocolo.
    # El protocolo se añadirá más tarde.
    gui = ChatGUI(protocol=None, my_nick=args.nick, db=db)
    
    # --- Inicializar Protocolo ---
    loop = asyncio.get_running_loop()
    transport, protocol_instance = await loop.create_datagram_endpoint(
        lambda: SecureIMProtocol(dnie_manager, gui.on_protocol_msg),
        local_addr=('0.0.0.0', args.port)
    )
    gui.protocol = protocol_instance # Asignar la instancia del protocolo a la GUI

    # --- Inicializar Discovery ---
    # La función de callback para el discovery cuando se encuentra un peer
    def on_peer_found(name, ip, port):
        # El nombre que viene de Zeroconf es 'nick_puerto', gui.py lo gestiona
        gui.add_or_update_peer(name, ip, port, from_zeroconf=True) 
    
    discovery_service = DiscoveryService(args.port, args.nick, on_peer_found)
    await discovery_service.start()

    # --- Arrancar GUI y manejar interrupciones ---
    with patch_stdout():
        # Handle Ctrl+C gracefully
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        # Handle Ctrl+C for Windows and Linux
        if os.name == 'nt': # Windows
            pass # Windows doesn't easily support signal.SIGINT for asyncio
        else: # Linux, macOS
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)

        try:
            await asyncio.gather(
                gui.run(),
                stop_event.wait() # Wait for Ctrl+C or app.exit()
            )
        except Exception as e:
            print(f"Error en la aplicación principal: {e}")
        finally:
            print("\nCerrando servicios...")
            await discovery_service.stop()
            transport.close()
            # Cierra la aplicación de prompt_toolkit si aún no lo ha hecho
            if not gui.app.is_exited:
                gui.app.exit()


if __name__ == "__main__":
    asyncio.run(main())