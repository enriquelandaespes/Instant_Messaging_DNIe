# discovery.py
import socket
import asyncio
from zeroconf import ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
import config

def get_lan_ip():
    """
    Obtiene la IP real de la interfaz de red (WiFi/Ethernet).
    Intenta método 1 (conectar fuera) y si falla, método 2 (listar interfaces).
    """
    # Método 1: Intentar ver qué IP usa para salir a internet
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # No envía nada, solo consulta la tabla de enrutamiento
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass

    # Método 2: Si falla lo anterior (ej. sin internet), listar todas las IPs
    try:
        hostname = socket.gethostname()
        # gethostbyname_ex devuelve (hostname, aliaslist, ipaddrlist)
        _, _, ips = socket.gethostbyname_ex(hostname)
        # Filtramos las de loopback (127.x.x.x)
        valid_ips = [ip for ip in ips if not ip.startswith("127.")]
        if valid_ips:
            return valid_ips[0] # Devolver la primera IP real encontrada
    except Exception:
        pass

    return '127.0.0.1' # Fallback final

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
        local_ip = get_lan_ip()
        print(f"--- [Discovery] Usando IP: {local_ip} ---")
        
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
                ip = None
                # Priorizar IPv4 (4 bytes)
                for addr in info.addresses:
                    if len(addr) == 4:
                        ip = socket.inet_ntoa(addr)
                        break
                
                if not ip:
                    print(f"--- [Discovery] No se encontró IPv4 para {name} ---")
                    return

                port = info.port
                
                # Limpiar nombre de forma segura: remover el sufijo del tipo de servicio
                # Se asume que name termina en .SERVICE_TYPE
                suffix = f".{config.SERVICE_TYPE}"
                if name.endswith(suffix):
                    clean_name = name[:-len(suffix)]
                else:
                    # Fallback si no coincide exactamente el sufijo
                    clean_name = name.replace(suffix, "").rstrip(".")

                self.on_peer(clean_name, ip, port)
        except Exception as e:
            print(f"--- [Discovery] Error resolviendo {name}: {e} ---")

    async def stop(self):
        if self.browser: await self.browser.async_cancel()
        if self.azc: await self.azc.async_close()