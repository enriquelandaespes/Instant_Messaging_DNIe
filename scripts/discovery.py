# discovery.py
import socket
import asyncio
import uuid
from zeroconf import ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
import config

class DiscoveryService:
    def __init__(self, my_port, my_nick, on_peer_found_callback):
        self.port = my_port
        self.nick = my_nick
        self.on_peer = on_peer_found_callback
        self.azc = None
        self.browser = None
        self.my_ip = self.get_lan_ip()
        
        # ID único para evitar choques
        unique_id = str(uuid.uuid4())[:8]
        self.my_name = f"dni-im-{unique_id}.{config.SERVICE_TYPE}"

    def get_lan_ip(self):
        """Detecta la IP real de la LAN"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'

    async def start(self):
        print(f"🌐 [mDNS] Usando interfaz: {self.my_ip}")
        
        # --- CORRECCIÓN CRÍTICA: FORZAR INTERFAZ ---
        try:
            self.azc = AsyncZeroconf(interfaces=[self.my_ip])
        except Exception:
            print("⚠️ Error binding IP específica, usando default")
            self.azc = AsyncZeroconf()
        # -------------------------------------------
        
        info = ServiceInfo(
            config.SERVICE_TYPE,
            self.my_name,
            addresses=[socket.inet_aton(self.my_ip)],
            port=self.port,
            properties={"nick": self.nick},
            server=f"{socket.gethostname()}.local.",
        )
        
        try:
            await self.azc.async_register_service(info)
            print(f"📢 [mDNS] Anunciando: {self.my_name}")
        except Exception as e:
            print(f"⚠️ [mDNS] Error registro: {e}")
        
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf, config.SERVICE_TYPE, handlers=[self._on_change]
        )

    def _on_change(self, zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added: return
        if name == self.my_name: return
        
        asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            found = await info.async_request(zeroconf, 3000)
            
            if found and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port
                
                peer_nick = name.split(".")[0]
                if info.properties and b'nick' in info.properties:
                    try: peer_nick = info.properties[b'nick'].decode('utf-8')
                    except: pass

                # Filtro propio IP/Puerto
                if ip == self.my_ip and port == self.port: return

                self.on_peer(peer_nick, ip, port)
        except Exception: pass

    async def stop(self):
        if self.browser: await self.browser.async_cancel()
        if self.azc: await self.azc.async_close()