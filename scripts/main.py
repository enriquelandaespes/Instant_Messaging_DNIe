# main.py
import asyncio
import sys
import os
import signal
import getpass # <-- Importamos getpass
import pkcs11.exceptions # Para capturar errores específicos del DNIe

from prompt_toolkit.patch_stdout import patch_stdout

from dnie_manager import DNIeManager
from discovery import DiscoveryService
from protocol import SecureIMProtocol
from gui import ChatGUI 
from database import JsonEncryptedDB # <-- Importamos la BD cifrada

# --- Configuración fija (puedes moverla a config.py si existe) ---
DEFAULT_PORT = 6666

async def main():
    print("Iniciando DNIe Secure IM...")

    # --- Variables para DNIe y BD ---
    dnie_manager = None
    db = None
    my_nick = "Desconocido"
    
    try:
        # --- Pedir PIN al usuario (ocultando la entrada) ---
        pin = getpass("Introduce el PIN de tu DNIe: ").strip() 
        if not pin:
            print("PIN no puede estar vacío. Saliendo.")
            return

        print("Cargando DNIe...")
        dnie_manager = DNIeManager(pin) # Inicializamos DNIeManager con el PIN

        # --- Obtener Nick y ID único del DNIe ---
        my_nick = dnie_manager.get_user_name() # dnie_manager.get_cn()
        db_id = dnie_manager.get_unique_id() 
        
        print(f"Usuario DNIe: {my_nick} (ID BD: {db_id[:8]}...), Puerto: {DEFAULT_PORT}")

        # --- Inicializar Database Manager (Cifrada con DNIe) ---
        db = JsonEncryptedDB(dnie_manager, db_id)

    except pkcs11.exceptions.PinIncorrect:
        print("\n❌ Error: El PIN introducido es incorrecto.")
        if dnie_manager: dnie_manager.close_session()
        return
    except pkcs11.exceptions.CardNotPresent:
        print("\n❌ Error: No se detecta el DNIe. Insértalo correctamente.")
        if dnie_manager: dnie_manager.close_session()
        return
    except Exception as e:
        print(f"\n❌ Error Crítico al iniciar DNIe/BD: {e}")
        # import traceback; traceback.print_exc() # Descomentar para ver detalles
        if dnie_manager: dnie_manager.close_session()
        return

    # --- Flujo normal si DNIe y BD se inicializaron correctamente ---
    loop = asyncio.get_running_loop()

    # Primero inicializamos GUI y Protocolo (Protocolo necesita DNIeManager y callback a GUI)
    # y la GUI necesita el protocolo y la DB.
    # Tenemos que crear instancias y luego conectarlas.
    protocol_instance = SecureIMProtocol(dnie_manager, None) # Callback se asigna después
    gui = ChatGUI(protocol=protocol_instance, my_nick=my_nick, db=db)
    protocol_instance.callback = gui.on_protocol_msg # Ahora asignamos el callback del protocolo a la GUI

    # --- Inicializar Discovery ---
    # El callback de discovery llama a la GUI
    discovery_service = DiscoveryService(DEFAULT_PORT, my_nick, gui.add_or_update_peer)
    await discovery_service.start()

    # --- Arrancar servidor UDP para el protocolo ---
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol_instance, 
        local_addr=('0.0.0.0', DEFAULT_PORT)
    )
    
    # --- Arrancar GUI y manejar interrupciones ---
    with patch_stdout():
        stop_event = asyncio.Event()

        if os.name == 'nt': 
            pass # Windows no maneja señales de SIGINT directamente en asyncio fácilmente
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
            print("\nCerrando servicios y DNIe...")
            await discovery_service.stop()
            transport.close()
            # Asegurarse de que dnie_manager está definido antes de intentar cerrar la sesión
            if dnie_manager: 
                # dnie_manager ya no tiene close_session en la última versión que te di,
                # la sesión se cierra automáticamente con el `with token.open()`
                # Pero si tienes una versión anterior con `_dnie_session` abierto,
                # deberías llamarlo. Por ahora, asumimos que no es necesario.
                pass 
            if not gui.app.is_exited:
                gui.app.exit()


if __name__ == "__main__":
    # Fix para Windows y selectores asíncronos
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Esto capturará Ctrl+C si no fue manejado por stop_event (ej. en Windows)
        print("\nPrograma terminado por usuario (Ctrl+C).")
        pass