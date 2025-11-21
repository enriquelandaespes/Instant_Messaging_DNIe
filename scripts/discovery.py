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
        
        # ID único para evitar choques de nombre en la red
        unique_id = str(uuid.uuid4())[:8]
        self.my_name = f"dni-im-{unique_id}.{config.SERVICE_TYPE}"

    def get_lan_ip(self):
        """Detecta la IP real de la LAN para anunciar dónde escuchamos."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'

    async def start(self):
        print(f"🌐 [mDNS] Discovery iniciado (IP visible: {self.my_ip})")
        
        # CORRECCIÓN: Usamos AsyncZeroconf() sin argumentos.
        # Forzar interfaces=[self.my_ip] causaba el fallo de recepción en Windows.
        try:
            self.azc = AsyncZeroconf()
        except Exception as e:
            print(f"⚠️ Error crítico mDNS: {e}")
            return

        # Información de nuestro servicio
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
            # print(f"📢 [mDNS] Anunciando: {self.my_name}")
        except Exception as e:
            print(f"--- [Discovery] Error al anunciar: {e} ---")
        
        # Escuchamos (Browser)
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf, config.SERVICE_TYPE, handlers=[self._on_change]
        )

    def _on_change(self, zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added: return
        if name == self.my_name: return # Ignorarnos a nosotros mismos
        
        # Resolvemos en background
        asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            # Timeout de 2s es suficiente para LAN y mejora la respuesta
            found = await info.async_request(zeroconf, 2000)
            
            if found and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port
                
                # Intentar extraer nick
                peer_nick = name.split(".")[0]
                if info.properties and b'nick' in info.properties:
                    try: peer_nick = info.properties[b'nick'].decode('utf-8')
                    except: pass

                # Filtro: No añadirnos a nosotros mismos si la IP coincide
                if ip == self.my_ip and port == self.port: return

                # Callback a la GUI
                self.on_peer(peer_nick, ip, port)
        except Exception: 
            pass

    async def stop(self):
        if self.browser: 
            await self.browser.async_cancel()
        if self.azc: 
            await self.azc.async_close()