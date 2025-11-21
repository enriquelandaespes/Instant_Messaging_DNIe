# discovery.py
import socket
import asyncio
import uuid  # <--- ¡ESTA ES LA LÍNEA QUE FALTABA!
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
        
        # ID único para evitar choques si reinicias rápido el programa
        unique_id = str(uuid.uuid4())[:8]
        self.my_name = f"dni-im-{unique_id}.{config.SERVICE_TYPE}"

    def get_lan_ip(self):
        """Detecta la IP real de la LAN para anunciar correctamente."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # No se envía nada, solo se calcula la ruta hacia Google DNS
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'

    async def start(self):
        print(f"🌐 [mDNS] Iniciando discovery en IP: {self.my_ip}")
        
        # Usamos AsyncZeroconf sin forzar interfaz específica para evitar errores en Windows
        try:
            self.azc = AsyncZeroconf()
        except Exception as e:
            print(f"⚠️ Error crítico mDNS: {e}")
            # Intentamos fallback sin argumentos si falla algo raro
            self.azc = AsyncZeroconf()

        # Información de nuestro servicio
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
        except Exception as e:
            print(f"⚠️ [mDNS] Error registro: {e}")
        
        # Empezamos a escuchar
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf, config.SERVICE_TYPE, handlers=[self._on_change]
        )

    def _on_change(self, zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added: return
        if name == self.my_name: return # Ignorarnos a nosotros mismos
        
        # Resolvemos el nombre encontrado en segundo plano
        asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            # Esperamos hasta 2s para resolver
            found = await info.async_request(zeroconf, 2000)
            
            if found and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port
                
                # Intentar extraer nick de las propiedades
                peer_nick = name.split(".")[0]
                if info.properties and b'nick' in info.properties:
                    try: peer_nick = info.properties[b'nick'].decode('utf-8')
                    except: pass

                # Filtro: No añadirnos a nosotros mismos
                if ip == self.my_ip and port == self.port: return

                # Avisar a la GUI
                self.on_peer(peer_nick, ip, port)
        except Exception: 
            pass

    async def stop(self):
        if self.browser: await self.browser.async_cancel()
        if self.azc: await self.azc.async_close()