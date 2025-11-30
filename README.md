# 💬 Instant Messaging DNIe — Mensajería P2P con Autenticación Hardware

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.8+-green.svg)
![Status](https://img.shields.io/badge/status-stable-brightgreen.svg)
![Security](https://img.shields.io/badge/security-DNIe%20%2B%20Noise%20IK-red.svg)

---

## 🎯 ¿Qué es esto?

**Instant Messaging DNIe** es un cliente de mensajería instantánea **peer-to-peer** que implementa seguridad de nivel profesional mediante el **DNI electrónico español**. Sin servidores centralizados, sin intermediarios, sin metadatos expuestos. Solo tú, tu DNIe, y comunicación cifrada punto a punto.

### 🔐 Características clave

- **Autenticación hardware**: Tu identidad vive en el chip del DNIe, nunca sale de ahí
- **Zero-knowledge**: No hay servidores que almacenen tus mensajes o contactos
- **Cifrado militar**: Noise Protocol Framework (mismo núcleo que WireGuard)
- **Descubrimiento automático**: Encuentra usuarios en tu red local sin configuración
- **Base de datos cifrada**: Incluso si roban tu ordenador, sin el DNIe no hay acceso
- **Reconexión inteligente**: Se restauran sesiones automáticamente tras desconexiones

### ⚡ Tecnologías implementadas

- **Noise IK**: Handshake criptográfico con forward secrecy
- **X25519**: Intercambio de claves con curva elíptica
- **ChaCha20-Poly1305**: Cifrado autenticado de mensajes
- **BLAKE2s**: Función hash rápida y segura
- **PKCS#11**: Acceso directo al chip criptográfico del DNIe
- **mDNS/Zeroconf**: Descubrimiento de servicios en red local
- **AES-GCM**: Cifrado de base de datos local

---

## 📋 Funcionalidades

| Funcionalidad                  | Estado        | Detalles Técnicos                |
|-------------------------------|--------------|----------------------------------|
| Identidad hardware (DNIe)     | ✅            | PKCS#11, firma en chip           |
| Descubrimiento mDNS           | ✅            | `_dni-im._udp.local.` (UDP 5353) |
| Sesión segura Noise IK        | ✅            | Noise IK con X25519, ChaCha20, BLAKE2s |
| Puerto único UDP              | ✅            | Todo handshake/datos por UDP 443 |
| Multiplexación con CID        | ✅            | Connection ID anti-spoofing      |
| TUI multi-chat                | ✅            | Interfaz terminal avanzada       |
| Agenda cifrada                | ✅            | AES-GCM, clave derivada DNIe     |
| Mensajes pendientes           | ✅            | Buffer offline persistente       |
| Reconexión automática         | ✅            | Restore de sesión sin rehandshake|
| Multi-usuario                 | ✅            | Una BD cifrada por DNIe          |
| ASCII Art                     | ✅            | Biblioteca integrada             |

---

## 🌐 Arquitectura general

```
┌─────────────────────┐
│    Usuario          │
│   (DNIe + PIN)      │
└───────┬─────────────┘
        │
┌───────▼─────────────┐        ┌─────────────┐
│  dnie_manager.py    │─────→  │  PKCS#11    │
│ (autenticación,     │        │  OpenSC     │
│  firma, certificados│        └─────────────┘
└───────┬─────────────┘
        │
┌───────▼──────────────┐
│  discovery.py        │
│  (mDNS/zeroconf)     │
│  _dni-im._udp.local. │
└───────┬──────────────┘
        │
┌───────▼─────────────────────────────────────────────────────┐
│  protocol.py (Noise IK + CID + ChaCha20-Poly1305)           │
│  UDP 443: Handshake, Mensajes, ACK, Reconexión             │
└───────┬─────────────────────────────────────────────────────┘
        │
┌───────▼─────────────┐    ┌──────────────┐
│ tui.py (TUI)        │◄──►│ database.py  │
│ prompt_toolkit      │    │ (Agenda, BD) │
│ Multi-chat UI       │    │ AES-GCM      │
└─────────────────────┘    └──────────────┘
```

---

## 🛠️ Instalación y requisitos

### Requisitos previos

- **Python 3.8+**
- **DNIe + lector de tarjetas inteligentes**
- **OpenSC** instalado en el sistema
  - Windows: [OpenSC-0.23.0](https://github.com/OpenSC/OpenSC/releases)
  - Linux: `sudo apt install opensc`
  - macOS: `brew install opensc`

### Dependencias Python

Instala todas las dependencias necesarias:

```bash
pip install prompt_toolkit zeroconf noiseprotocol cryptography python-pkcs11 asyncio
```

O usando el archivo de requisitos (si existe):

```bash
pip install -r requirements.txt
```

### Configuración

1. **Verifica que OpenSC esté instalado correctamente:**
   ```bash
   # Windows (PowerShell)
   Test-Path "C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll"
   
   # Linux
   ls /usr/lib/x86_64-linux-gnu/opensc-pkcs11.so
   
   # macOS
   ls /usr/local/lib/opensc-pkcs11.so
   ```

2. **Ajusta la ruta de PKCS#11 en `config.py` si es necesario:**
   ```python
   # Para Linux
   PKCS11_LIB_PATH = "/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so"
   
   # Para macOS
   PKCS11_LIB_PATH = "/usr/local/lib/opensc-pkcs11.so"
   ```

3. **Conecta tu DNIe al lector de tarjetas**

---

## 🚀 Uso

### Arranque básico

```bash
cd scripts
python main.py
```

### Especificar puerto personalizado

```bash
python main.py 6666
```

### Primer uso

1. **Introduce el PIN del DNIe** cuando se solicite
2. La aplicación leerá automáticamente tu certificado
3. Comenzará a anunciar tu presencia en la red local vía mDNS
4. Otros usuarios aparecerán automáticamente en tu lista de contactos

---

## 🖥️ Interfaz TUI — Guía de uso

### Navegación principal

| Tecla(s)      | Acción                                    |
|--------------|-------------------------------------------|
| `↑` / `↓`    | Cambiar entre contactos/chats            |
| `Tab`        | Alternar entre campo Chat y ASCII Art     |
| `Enter`      | Enviar mensaje / Conectar con usuario     |
| `Ctrl+C`     | Salir de la aplicación                    |
| `Ctrl+D`     | Desconectar del usuario actual            |

### Navegación en el chat

| Tecla(s)        | Acción                                  |
|----------------|-----------------------------------------|
| `Shift+↑`      | Scroll hacia arriba (5 líneas)          |
| `Shift+↓`      | Scroll hacia abajo (5 líneas)           |

### Pestañas especiales

- **❓ Ayuda**: Atajos de teclado y guía rápida
- **👤 Mi Cuenta**: Información de tu DNIe, IP, puerto y estadísticas

### Estados de conexión

| Icono | Estado        | Descripción                              |
|-------|--------------|------------------------------------------|
| 🟢    | Conectado    | Sesión activa y encriptada               |
| 🔴    | Desconectado | Usuario conocido pero offline            |
| 🟡    | Disponible   | Descubierto vía mDNS, no conectado       |
| ⏳    | Conectando   | Handshake Noise IK en proceso            |

### Estados de mensajes

| Icono | Estado      | Descripción                               |
|-------|------------|-------------------------------------------|
| 🕒    | Enviando   | Mensaje en cola o esperando ACK           |
| ✅    | Entregado  | Confirmado por el destinatario            |
| 🔔    | No leído   | Mensajes recibidos pendientes de leer     |

---

## 🎨 ASCII Art

El sistema incluye una biblioteca de arte ASCII integrada:

1. Presiona **Tab** para cambiar al campo ASCII
2. Escribe parte del nombre (ej: `rifle`, `heart`, `cat`)
3. Aparecerán sugerencias automáticamente
4. Presiona **Enter** para enviarlo

El archivo `ascii.json` contiene la biblioteca completa y es extensible.

---

## 🔒 Seguridad y criptografía

### Autenticación DNIe (PKCS#11)

- Todo el proceso de firma y autenticación ocurre **dentro del chip del DNIe**
- La clave privada **nunca sale** de la tarjeta
- Se usa el certificado de autenticación del DNIe

### Protocolo Noise IK

```
Handshake Noise IK:
─────────────────────────────────────────────────────────────
Initiator                                        Responder
─────────────────────────────────────────────────────────────
static_private_key (derivada de DNIe)           static_private_key
static_public_key                                static_public_key

        ──── e, es, s, ss ────────────>
        [clave efímera + cert DNIe]

        <──── e, ee, se ──────────────
              [respuesta + cert DNIe]

✅ Sesión establecida con ChaCha20-Poly1305
```

### Cifrado de mensajes

- **Algoritmo**: ChaCha20-Poly1305 (AEAD)
- **Derivación de claves**: BLAKE2s
- **Intercambio de claves**: X25519 (curva elíptica)
- **Nonce**: 12 bytes aleatorios por mensaje

### Base de datos cifrada

```
1. Challenge C (64 bits) almacenado en disco
2. C firmado con DNIe → S
3. K = SHA256(S)
4. K_db (256 bits) cifrada con K usando AES-GCM
5. Base de datos cifrada con K_db
```

**Resultado**: Sin el DNIe correcto, imposible descifrar la agenda.

---

## 📁 Estructura del proyecto

```
Instant_Messaging_DNIe/
├── scripts/
│   ├── main.py              # Punto de entrada
│   ├── config.py            # Configuración global
│   ├── dnie_manager.py      # Gestión DNIe y PKCS#11
│   ├── discovery.py         # mDNS y descubrimiento
│   ├── protocol.py          # Noise IK, UDP, handshake
│   ├── database.py          # Agenda cifrada
│   ├── tui.py               # Interfaz de usuario TUI
│   └── ascii.json           # Biblioteca ASCII Art
├── README.md
├── PROCESO_CREATIVO_IA.md
└── requirements.txt
```

---

## 🔧 Protocolo de red

### Tipos de paquetes (8 tipos)

| Tipo | Nombre              | Descripción                          |
|------|---------------------|--------------------------------------|
| 0x01 | `HANDSHAKE_INIT`    | Inicio handshake Noise IK            |
| 0x02 | `MSG`               | Mensaje cifrado                      |
| 0x03 | `HANDSHAKE_RESP`    | Respuesta handshake Noise IK         |
| 0x04 | `ACK`               | Confirmación de mensaje              |
| 0x05 | `RECONNECT_REQ`     | Solicitud de reconexión              |
| 0x06 | `RECONNECT_RESP`    | Respuesta reconexión                 |
| 0x07 | `PENDING_SEND`      | Inicio envío mensajes pendientes     |
| 0x08 | `PENDING_DONE`      | Fin envío mensajes pendientes        |

### Formato de paquete

```
┌──────────┬──────────┬──────────────────┐
│  Tipo    │   CID    │     Payload      │
│  1 byte  │  4 bytes │   Variable       │
└──────────┴──────────┴──────────────────┘
```

- **CID (Connection ID)**: Identificador único de 4 bytes para multiplexación
- **Verificación anti-spoofing**: Los CIDs se validan contra la dirección IP/puerto esperada

---

## 🧪 Testing y troubleshooting

### Problemas comunes

#### DNIe no detectado

```
Error: No se detecta tarjeta DNIe.
```

**Solución**:
1. Verifica que el lector esté conectado
2. Comprueba que OpenSC está instalado correctamente
3. Prueba con otra aplicación PKCS#11 para descartar problemas hardware

#### PIN bloqueado

```
Error: PIN incorrecto (intentos restantes: 0)
```

**Solución**: Usa el software oficial del DNIe para desbloquear con el PUK.

#### No se descubren usuarios en la red

**Soluciones**:
1. Verifica que el firewall permite UDP 443 y UDP 5353
2. Comprueba que todos los equipos están en la misma red local
3. En Windows, permite la app en "Windows Defender Firewall"
4. Desactiva temporalmente VPNs que puedan interferir

#### Error de permisos en Linux

```bash
# Añadir usuario al grupo necesario
sudo usermod -a -G plugdev $USER
sudo usermod -a -G scard $USER

# Reiniciar sesión
```

### Logging y debug

Para activar logs detallados, modifica `main.py`:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

---

## 🎯 Interoperabilidad (Connectathon)

Para asegurar compatibilidad con otros equipos:

1. **Puerto**: Todos deben usar UDP **443** (valor por defecto)
2. **mDNS**: Servicio `_dni-im._udp.local.`
3. **Noise IK**: Implementación estándar con librería oficial
4. **Formato de paquetes**: Según especificación del protocolo

### Checklist pre-Connectathon

- [ ] Firewall configurado para UDP 443 y 5353
- [ ] DNIe con PIN funcional
- [ ] Otros equipos visibles en la misma red
- [ ] Handshake exitoso con al menos un peer
- [ ] Mensajes cifrados enviados y recibidos correctamente

---

## 🚧 Desarrollo

### Arquitectura asíncrona

Todo el stack de red usa `asyncio`:

```python
# Protocolo UDP asíncrono
class SecureIMProtocol(asyncio.DatagramProtocol):
    ...

# Event loop principal
asyncio.run(main())
```

### Añadir nuevos tipos de paquete

1. Define el tipo en `protocol.py`:
   ```python
   PKT_NEW_TYPE = 0x09
   ```

2. Añade el handler en `datagram_received()`:
   ```python
   elif msg_type == PKT_NEW_TYPE:
       self.handle_new_type(payload, addr)
   ```

3. Implementa la lógica del handler

### Base de datos

La estructura de la base de datos:

```json
{
  "contacts": {
    "contact_id": {
      "name": "Nombre Usuario",
      "ip": "192.168.1.100",
      "port": 443,
      "msgs": [...],
      "is_connected": true,
      "session_key": "hex_encoded_key",
      "peer_cert": "hex_encoded_cert",
      "peer_static_key": "hex_encoded_static_key"
    }
  }
}
```

---

## 📚 Referencias técnicas

- [Noise Protocol Framework](https://noiseprotocol.org/noise.html)
- [Noise IK Pattern](https://noiseprotocol.org/noise.html#interactive-patterns)
- [OpenSC PKCS#11](https://github.com/OpenSC/OpenSC/wiki)
- [Python Zeroconf](https://github.com/jstasiak/python-zeroconf)
- [prompt_toolkit Documentation](https://python-prompt-toolkit.readthedocs.io/)
- [ChaCha20-Poly1305](https://tools.ietf.org/html/rfc8439)
- [X25519 Key Agreement](https://tools.ietf.org/html/rfc7748)

---

**⚡ Comunicación segura sin intermediarios • Zero-knowledge • Autenticación hardware ⚡**
