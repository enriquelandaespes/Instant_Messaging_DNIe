# discovery.py
import socket
import asyncio
from zeroconf import ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
import config

def get_lan_ip():
    """Obtiene la IP real de la LAN (WiFi/Ethernet)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

class DiscoveryService:
    def __init__(self, my_port, my_nick, on_peer_found_callback):
        self.port = my_port
        self.nick = my_nick
        self.on_peer = on_peer_found_callback
        self.azc = None
        self.browser = None
        self.my_ip = get_lan_ip()
        
        # Nombre único para evitar colisiones: Nick_Puerto
        self.my_name = f"{self.nick}_{self.port}.{config.SERVICE_TYPE}"

    async def start(self):
        # Iniciamos Zeroconf en todas las interfaces (por defecto)
        self.azc = AsyncZeroconf()
        
        print(f"DEBUG: Anunciando en {self.my_ip}")

        # Anunciamos nuestra IP REAL, no localhost
        info = ServiceInfo(
            config.SERVICE_TYPE,
            self.my_name,
            addresses=[socket.inet_aton(self.my_ip)],
            port=self.port,
            properties={'version': '1.0'},
        )
        await self.azc.async_register_service(info)
        
        # Escuchamos
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf, config.SERVICE_TYPE, handlers=[self._on_change]
        )

    def _on_change(self, zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added: return
        if name == self.my_name: return # Ignorarnos a nosotros mismos
        asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            # Damos 3 segundos para resolver
            if await info.async_request(zeroconf, 3000) and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port
                clean_name = name.split(".")[0] 
                # Avisamos a la GUI
                self.on_peer(clean_name, ip, port)
        except: pass

    async def stop(self):
        if self.browser: await self.browser.async_cancel()
        if self.azc: await self.azc.async_close()