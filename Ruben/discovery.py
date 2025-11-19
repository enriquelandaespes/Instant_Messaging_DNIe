# Ruben/discovery.py
import socket
import asyncio
from zeroconf import ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
import config

# --- FUNCIÓN AÑADIDA PARA SACAR LA IP REAL ---
def get_lan_ip():
    """
    Intenta conectar a una IP pública (Google DNS) para ver
    qué interfaz de red usa el SO para salir a internet.
    No envía datos reales.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No hace falta que 8.8.8.8 sea accesible, solo calcula la ruta
        s.connect(('8.8.8.8', 80))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP
# ---------------------------------------------

class DiscoveryService:
    def __init__(self, my_port, my_nick, on_peer_found_callback):
        self.port = my_port
        self.nick = my_nick
        self.on_peer = on_peer_found_callback
        self.azc = None
        self.browser = None
        self.seen = set()
        
        # Generar nombre único
        self.my_name = f"{self.nick}_{self.port}.{config.SERVICE_TYPE}"

    async def start(self):
        self.azc = AsyncZeroconf()
        
        # CAMBIO AQUÍ: Usamos la función robusta en lugar de gethostname
        local_ip = get_lan_ip()
        print(f"[Discovery] Anunciando en la IP: {local_ip}") # Log para que verifiques

        # Anunciar
        info = ServiceInfo(
            config.SERVICE_TYPE,
            self.my_name,
            addresses=[socket.inet_aton(local_ip)],
            port=self.port,
            properties={},
        )
        await self.azc.async_register_service(info)
        
        # Escuchar
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf, config.SERVICE_TYPE, handlers=[self._on_change]
        )

    def _on_change(self, zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added: return
        if name == self.my_name: return # Ignorarnos
        asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            if await info.async_request(zeroconf, 3000) and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port
                clean_name = name.split(".")[0] # Limpiar nombre
                self.on_peer(clean_name, ip, port)
        except: pass

    async def stop(self):
        if self.browser: await self.browser.async_cancel()
        if self.azc: await self.azc.async_close()