# discovery.py
import socket
import asyncio
from zeroconf import ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
import config

def get_lan_ip():
    """
    Obtiene la IP real que sale a internet (WiFi/Ethernet).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Conectamos a una IP pública (no envía datos, solo consulta tabla de rutas)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

class DiscoveryService:
    def __init__(self, my_port, my_nick, on_peer_found_callback):
        self.port = my_port
        self.nick = my_nick
        self.on_peer = on_peer_found_callback
        self.azc = None
        self.browser = None
        self.my_ip = get_lan_ip()
        
        # Nombre único del servicio
        self.my_name = f"{self.nick}_{self.port}.{config.SERVICE_TYPE}"

    async def start(self):
        print(f"--- [Discovery] Iniciando en IP: {self.my_ip} ---")
        
        try:
            # IMPORTANTE: Forzamos la interfaz de la IP real. 
            # Esto arregla el problema de que no aparezcan los vecinos en Windows.
            self.azc = AsyncZeroconf(interfaces=[self.my_ip])
        except Exception as e:
            print(f"--- [Discovery] Error bind IP {self.my_ip}, usando default: {e}")
            self.azc = AsyncZeroconf() # Fallback por si acaso

        # Preparamos la información del servicio
        info = ServiceInfo(
            config.SERVICE_TYPE,
            self.my_name,
            addresses=[socket.inet_aton(self.my_ip)],
            port=self.port,
            properties={'version': '1.0', 'nick': self.nick},
        )

        # Anunciamos (Register)
        try:
            await self.azc.async_register_service(info)
            print(f"--- [Discovery] Anunciando: {self.my_name} ---")
        except Exception as e:
            print(f"--- [Discovery] Error al anunciar: {e} ---")
        
        # Escuchamos (Browser)
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf, config.SERVICE_TYPE, handlers=[self._on_change]
        )

    def _on_change(self, zeroconf, service_type, name, state_change):
        # Si alguien aparece (Added)
        if state_change is ServiceStateChange.Added:
            if name == self.my_name:
                return # Ignorarnos a nosotros mismos
            
            # Resolvemos la info del vecino asíncronamente
            asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            # Esperamos hasta 3 segundos para obtener detalles
            if await info.async_request(zeroconf, 3000) and info.addresses:
                
                # Buscar una dirección IPv4 válida
                ip_str = None
                for addr in info.addresses:
                    if len(addr) == 4: 
                        ip_str = socket.inet_ntoa(addr)
                        break
                
                if ip_str:
                    port = info.port
                    # Limpiamos el nombre
                    clean_name = name.split(".")[0]
                    
                    # Avisamos a la GUI
                    self.on_peer(clean_name, ip_str, port)
        except Exception:
            pass

    async def stop(self):
        if self.browser: 
            await self.browser.async_cancel()
        if self.azc: 
            await self.azc.async_close()