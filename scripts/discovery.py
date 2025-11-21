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
        
        # Generamos un ID único para esta sesión para evitar choques si reinicias rápido
        unique_id = str(uuid.uuid4())[:8]
        self.my_name = f"dni-im-{unique_id}.{config.SERVICE_TYPE}"
        self.my_ip = self.get_lan_ip()

    def get_lan_ip(self):
        """Detecta la IP real de la interfaz conectada a Internet/LAN"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No se envía nada, solo se calcula la ruta
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = '127.0.0.1'
        finally:
            s.close()
        return ip

    async def start(self):
        print(f"🌐 [mDNS] Iniciando discovery en {self.my_ip}:{self.port}")
        self.azc = AsyncZeroconf()
        
        # Anunciar nuestra presencia
        info = ServiceInfo(
            config.SERVICE_TYPE,
            self.my_name,
            addresses=[socket.inet_aton(self.my_ip)],
            port=self.port,
            properties={"nick": self.nick}, # Guardamos el nick en las propiedades
            server=f"{socket.gethostname()}.local.",
        )
        
        try:
            await self.azc.async_register_service(info)
            print(f"📢 [mDNS] Anunciando: {self.my_name}")
        except Exception as e:
            print(f"⚠️ [mDNS] Error al registrar servicio: {e}")
        
        # Escuchar a otros
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf, config.SERVICE_TYPE, handlers=[self._on_change]
        )

    def _on_change(self, zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added: return
        if name == self.my_name: return # Ignorarnos a nosotros mismos
        
        # Resolvemos en segundo plano
        asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            # Esperamos hasta 3 segundos para resolver
            found = await info.async_request(zeroconf, 3000)
            
            if found and info.addresses:
                # Convertir bytes de IP a string
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port
                
                # Intentar sacar el nick de las propiedades o usar el nombre del servicio
                peer_nick = name.split(".")[0]
                if info.properties and b'nick' in info.properties:
                    try:
                        peer_nick = info.properties[b'nick'].decode('utf-8')
                    except: pass

                # Filtramos nuestra propia IP por si acaso
                if ip == self.my_ip and port == self.port:
                    return

                # print(f"🔭 [mDNS] Encontrado: {peer_nick} en {ip}:{port}") # Debug opcional
                self.on_peer(peer_nick, ip, port)
        except Exception as e:
            # print(f"Error resolviendo {name}: {e}")
            pass

    async def stop(self):
        print("🛑 [mDNS] Deteniendo servicio...")
        if self.browser: await self.browser.async_cancel()
        if self.azc: await self.azc.async_close()