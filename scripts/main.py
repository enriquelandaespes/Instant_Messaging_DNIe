# main.py
import asyncio
import sys
import os
import signal
from getpass import getpass 
import pkcs11.exceptions 

from prompt_toolkit.patch_stdout import patch_stdout

from dnie_manager import DNIeManager
from discovery import DiscoveryService
from protocol import SecureIMProtocol
from gui import ChatGUI 
from database import JsonEncryptedDB 

DEFAULT_PORT = 6666

async def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])

    print(f"--- DNIe CHAT (Puerto {port}) ---")

    dnie_manager = None
    db = None
    my_nick = "Desconocido"
    
    try:
        # 1. Pedir PIN de forma oculta
        pin = getpass("Introduce el PIN de tu DNIe: ").strip() 
        
        if not pin:
            print("PIN no puede estar vacío. Saliendo.")
            return

        print("⌛ Cargando DNIe...")
        dnie_manager = DNIeManager(pin) 

        # 2. Obtener Nick y ID único
        my_nick = dnie_manager.get_user_name()
        db_id = dnie_manager.get_unique_id() 
        
        print(f"✅ Usuario DNIe: {my_nick}")
        print(f"🔑 ID Base de Datos: {db_id[:8]}...")

        # 3. Inicializar Base de Datos Cifrada
        db = JsonEncryptedDB(dnie_manager, db_id)

    except pkcs11.exceptions.PinIncorrect:
        print("\n❌ Error: El PIN introducido es incorrecto.")
        return
    except pkcs11.exceptions.TokenNotPresent:
        print("\n❌ Error: No se detecta el DNIe. Insértalo correctamente.")
        return
    except Exception as e:
        print(f"\n❌ Error Crítico al iniciar: {e}")
        return

    # --- Inicio del sistema ---
    loop = asyncio.get_running_loop()
    
    # Definir callbacks
    def protocol_cb(addr, text, nombre):
        gui.on_protocol_msg(addr, text, nombre)

    def discovery_cb(name, ip, p):
        gui.add_or_update_peer(name, ip, p)

    # Inicializar componentes
    protocol_instance = SecureIMProtocol(dnie_manager, db, protocol_cb)
    gui = ChatGUI(protocol=protocol_instance, my_nick=my_nick, db=db)
    
    # Discovery
    discovery_service = DiscoveryService(port, my_nick, discovery_cb)
    await discovery_service.start()

    # Servidor UDP
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol_instance, 
        local_addr=('0.0.0.0', port)
    )
    
    # Arrancar GUI
    with patch_stdout():
        stop_event = asyncio.Event()

        # En Windows, asyncio no soporta add_signal_handler para SIGINT
        if os.name != 'nt': 
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)

        try:
            # Ejecutamos la GUI y esperamos el evento de parada
            # Usamos wait con FIRST_COMPLETED para que si la GUI se cierra (Ctrl+D), salgamos
            # Y si damos Ctrl+C (capturado abajo o por signal), también salgamos
            tasks = [
                asyncio.create_task(gui.run()),
                asyncio.create_task(stop_event.wait())
            ]
            # Esperar a que cualquiera de los dos termine
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            # Cancelar las tareas pendientes (la que no terminó)
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass # Ignorar error de cancelación

        except KeyboardInterrupt:
            # Captura Ctrl+C directo si no fue manejado por señales
            pass
        except Exception as e:
            print(f"Error en la aplicación principal: {e}")
        finally:
            print("\nCerrando servicios...")
            # 1. Parar discovery
            await discovery_service.stop()
            
            # 2. Cerrar socket UDP
            if transport:
                transport.close()
            
            # 3. Cerrar GUI si sigue corriendo
            # CORRECCIÓN: Usamos 'is_running' en lugar de 'is_exited'
            try:
                if gui.app.is_running:
                    gui.app.exit()
            except Exception:
                pass # Ignorar errores al cerrar la GUI si ya estaba cerrada

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Captura final global para limpiar el traceback de 'asyncio.run'
        pass
    except Exception:
        # Evitar ruido extra al salir
        pass