# discovery.py
import socket
import asyncio
from zeroconf import ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
import config

def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        _, _, ips = socket.gethostbyname_ex(hostname)
        valid_ips = [ip for ip in ips if not ip.startswith("127.")]
        if valid_ips: return valid_ips[0]
    except Exception:
        pass
    return '127.0.0.1'

class DiscoveryService:
    def __init__(self, my_port, my_nick, on_peer_found_callback):
        self.port = my_port
        self.nick = my_nick
        self.on_peer = on_peer_found_callback
        self.azc = None
        self.browser = None
        
        # CORRECCIÓN: No quitamos espacios para respetar el formato del DNI
        # Zeroconf suele manejar bien los espacios en los nombres de servicio
        self.my_name = f"{self.nick}_{self.port}.{config.SERVICE_TYPE}"

    async def start(self):
        self.azc = AsyncZeroconf()
        local_ip = get_lan_ip()
        print(f"--- [Discovery] IP: {local_ip} | ID: {self.my_name} ---")
        
        info = ServiceInfo(
            config.SERVICE_TYPE,
            self.my_name,
            addresses=[socket.inet_aton(local_ip)],
            port=self.port,
            properties={},
        )
        await self.azc.async_register_service(info)
        
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf, config.SERVICE_TYPE, handlers=[self._on_change]
        )

    def _on_change(self, zeroconf, service_type, name, state_change):
        if name == self.my_name: return 

        if state_change is ServiceStateChange.Added:
            asyncio.create_task(self._resolve(zeroconf, service_type, name))
        
        elif state_change is ServiceStateChange.Removed:
            # Detectar desconexión
            clean_name = self._clean_service_name(name)
            self.on_peer(clean_name, None, None)

    def _clean_service_name(self, name):
        suffix = f".{config.SERVICE_TYPE}"
        if name.endswith(suffix):
            return name[:-len(suffix)]
        return name.replace(suffix, "").rstrip(".")

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            if await info.async_request(zeroconf, 3000) and info.addresses:
                ip = None
                for addr in info.addresses:
                    if len(addr) == 4:
                        ip = socket.inet_ntoa(addr)
                        break
                if not ip: return
                
                clean_name = self._clean_service_name(name)
                self.on_peer(clean_name, ip, info.port)
        except Exception as e:
            print(f"Error discovery: {e}")

    async def stop(self):
        if self.browser: await self.browser.async_cancel()
        if self.azc: await self.azc.async_close()