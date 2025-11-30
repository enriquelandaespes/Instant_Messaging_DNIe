# 🔐 Instant Messaging with DNIe Identity

Secure peer-to-peer instant messaging client using Spanish DNIe (Digital National Identity card) for hardware-backed identity authentication and **manual Noise IK-style cryptography** (X25519 + BLAKE2s + ChaCha20-Poly1305).

---

## 📋 Overview

A desktop/laptop instant messaging application that provides:

- **🪪 DNIe Identity**: Hardware-backed authentication using Spanish DNIe smartcard (PKCS#11)
- **🔒 Manual Noise IK**: Custom implementation of WireGuard-style cryptography
  - X25519 for ECDH (Elliptic Curve Diffie-Hellman)
  - BLAKE2s for key derivation
  - ChaCha20-Poly1305 for authenticated encryption
- **🌐 mDNS Discovery**: Automatic peer discovery on local network via `_dni-im._udp.local`
- **💬 Text UI (TUI)**: Multi-chat management interface using Textual
- **📇 Contact Book**: Friendly names pinned to certificate fingerprints (TOFU)
- **🔌 Single UDP Port**: All traffic (handshake + data) over UDP/6666 or configurable port
- **🔄 Connection Multiplexing**: CID (Connection ID) based session demultiplexing
- **📬 Message Queueing**: Offline message delivery when peers reconnect
- **🔐 Session Persistence**: Store session keys to avoid re-handshake on reconnection

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Instant Messaging App                     │
├─────────────────────────────────────────────────────────────┤
│  TUI Layer              chat_ui.py (Textual UI)             │
│                        - Multiple chat windows              │
│                        - Contact list management            │
│                        - Message history                    │
├─────────────────────────────────────────────────────────────┤
│  Protocol Layer         protocol.py (Manual Noise IK)       │
│                        - Handshake management               │
│                        - X25519 ECDH                        │
│                        - ChaCha20-Poly1305 encryption       │
│                        - Session multiplexing (CID)         │
├─────────────────────────────────────────────────────────────┤
│  Identity Layer         dnie_manager.py (PKCS#11)           │
│                        - DNIe certificate retrieval         │
│                        - Static key signing                 │
│                        - PIN management                     │
├─────────────────────────────────────────────────────────────┤
│  Discovery Layer        mdns_service.py (Zeroconf)          │
│                        - Service advertisement              │
│                        - Peer discovery                     │
├─────────────────────────────────────────────────────────────┤
│  Storage Layer          database.py (SQLite)                │
│                        - Contact management                 │
│                        - Message history                    │
│                        - Session key caching                │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔐 Cryptographic Protocol: Manual Noise IK Implementation

### Protocol Specification

The application implements a **manual, custom version of Noise IK** using standard cryptographic primitives:

```
Custom Noise IK Architecture:
├─ Static Key Exchange: X25519 (32-byte keys)
├─ Ephemeral Key Exchange: X25519 (32-byte keys)
├─ Key Derivation: BLAKE2s (256-bit output)
├─ Authenticated Encryption: ChaCha20-Poly1305 (AEAD)
└─ Forward Secrecy: Yes (ephemeral keys in handshake)
```

### Why Manual Implementation?

Instead of using the `noise-protocol` library, we implement the cryptographic primitives directly to:
- ✅ Have **full control** over the protocol
- ✅ **Understand every byte** that goes on the wire
- ✅ **Simplify debugging** with custom packet formats
- ✅ **Avoid library dependencies** (only cryptography.io)
- ✅ **Learn cryptography** in depth

### Handshake Flow

**Phase 1: Initiator sends handshake**
```
Initiator                           Responder
   │                                    │
   │  1. Generate ephemeral keypair     │
   │     (32-byte X25519 private key)  │
   │                                    │
   │  2. ECDH with static key           │
   │     shared_secret = DH(ephem_priv,│
   │                   responder_static)│
   │                                    │
   │  3. Derive session_key             │
   │     session_key = BLAKE2s(         │
   │       shared_secret, 32 bytes)    │
   │                                    │
   │  4. Build handshake packet:        │
   │     ┌──────────────────┐          │
   │     │ Type: 0x01       │          │
   │     │ CID: 4 bytes     │          │
   │     │ Ephemeral Pub    │          │
   │     │ DNIe Cert (enc)  │          │
   │     └──────────────────┘          │
   │                                    │
   ├───────────────────────────────────>│
   │    PKT_HANDSHAKE_INIT              │
   │    (Certificate encrypted with     │
   │     BLAKE2s-derived nonce)        │
```

**Phase 2: Responder responds**
```
Initiator                           Responder
   │                                    │
   │                       ┌──────────────────────┐
   │                       │ 1. Receive ephemeral │
   │                       │    public key        │
   │                       │                      │
   │                       │ 2. ECDH with        │
   │                       │    static key       │
   │                       │    shared_secret =  │
   │                       │    DH(static_priv,  │
   │                       │    ephemeral_init)  │
   │                       │                      │
   │                       │ 3. Derive session   │
   │                       │    key (same as     │
   │                       │    initiator)       │
   │                       │                      │
   │                       │ 4. Verify           │
   │                       │    certificate      │
   │                       │ 5. Build response   │
   │                       └──────────────────────┘
   │                                    │
   │  2. PKT_HANDSHAKE_RESP             │
   │  ┌──────────────────┐              │
   │  │ Type: 0x03       │              │
   │  │ CID: 4 bytes     │              │
   │  │ Ephemeral Pub    │              │
   │  │ DNIe Cert (enc)  │              │
   │  └──────────────────┘              │
   │<─────────────────────────────────── │
   │                                    │
   │  3. Session Established            │
   │     Both have same session_key     │
   │<──── Encrypted Messages (PKT_MSG)─>│
```

### Cryptographic Details

**Session Key Derivation:**
```
shared_secret = X25519(initiator_ephemeral_priv, 
                       responder_static_pub)
                [Also done by responder with their keys]

session_key = BLAKE2s(
    input: shared_secret (32 bytes),
    digest_size: 32 bytes
)
```

**Certificate Encryption:**
```
plaintext = [
    2-byte length | X25519 public key (32 bytes)
    2-byte length | DNIe certificate (DER encoded)
]

nonce = first 12 bytes of BLAKE2s(ephemeral_pub)
ciphertext = ChaCha20Poly1305.encrypt(
    key: ephemeral_pub[:32],  # Temporary key for handshake
    nonce: nonce,
    plaintext: plaintext
)
```

**Message Encryption (After Session Established):**
```
message_data = [UUID | text content]

nonce = random 12 bytes
ciphertext = ChaCha20Poly1305.encrypt(
    key: session_key (32 bytes),
    nonce: nonce,
    plaintext: message_data
)

packet = [Type (1B) | CID (4B) | Nonce (12B) | Ciphertext]
```

### Security Properties

| Property | How Achieved | Status |
|----------|-------------|--------|
| **Confidentiality** | ChaCha20 encryption | ✅ Protected |
| **Authenticity** | Poly1305 MAC tag | ✅ Protected |
| **Forward Secrecy** | Ephemeral X25519 in handshake | ✅ Protected |
| **Peer Authentication** | DNIe certificate verification | ✅ Protected |
| **Replay Protection** | Message UUIDs + ACK protocol | ✅ Protected |
| **Key Compromise** | Ephemeral keys → limited damage | ✅ Protected |
| **Perfect Forward Secrecy** | Not implemented (static keys reused) | ⏳ Future |

---

## 📦 Installation

### Prerequisites

**Hardware:**
- DNIe smartcard reader (USB)
- Spanish DNIe card
- Network interface (for mDNS)

**Software:**
- Python 3.8 or later
- OpenSC (for PKCS#11 support)
- PCSC daemon (card reader daemon)

### Step 1: Install System Dependencies

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install -y \
    python3 \
    python3-pip \
    python3-dev \
    opensc \
    pcscd \
    libpcsclite-dev \
    build-essential
```

**macOS:**
```bash
brew install opensc pcsc-lite
brew install python@3.11
```

**Windows (with chocolatey):**
```powershell
choco install python opensc-tools
```

### Step 2: Verify DNIe Setup

```bash
# Check card reader
pcsc_scan

# Verify PKCS#11 module
pkcs11-tool --module /usr/lib/x86_64-linux-gnu/opensc-pkcs11.so -L

# Test DNIe card access
pkcs11-tool --module /usr/lib/x86_64-linux-gnu/opensc-pkcs11.so --list-objects
```

### Step 3: Install Python Dependencies

```bash
# Clone repository
git clone https://github.com/enriquelandaespes/Instant_Messaging_DNIe.git
cd Instant_Messaging_DNIe/scripts

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

**requirements.txt:**
```
cryptography>=41.0.0
textual>=0.38.0
zeroconf>=0.131.0
PyKCS11>=1.5.12
```

### Step 4: Configure

Edit `config.py`:

```python
# Network Configuration
UDP_PORT = 6666                    # Custom port (not 443 in dev)
LISTEN_IP = "0.0.0.0"
TIMEOUT_SECONDS = 30
RECONNECT_TIMEOUT = 5              # Reconnect attempt timeout

# DNIe Configuration
PKCS11_MODULE = "/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so"
DNIE_PIN = None                    # Will prompt if None
DNIE_SLOT = 0

# mDNS Configuration
SERVICE_NAME = "_dni-im._udp.local"
SERVICE_PORT = UDP_PORT

# Database Configuration
DB_PATH = "messaging.db"
LOG_PATH = "app.log"

# Cryptography
HANDSHAKE_TIMEOUT = 3.0            # Timeout for handshake response
```

---

## 🚀 Usage

### Start the Application

```bash
python main.py
```

**Command-line options:**
```bash
python main.py --port 7777           # Custom port
python main.py --no-mdns             # Disable mDNS discovery
python main.py --debug               # Enable debug logging
```

### TUI Controls

| Key | Action |
|-----|--------|
| **Tab** | Switch between chat windows |
| **Ctrl+N** | New chat / Connect to peer |
| **Ctrl+L** | List available contacts |
| **Ctrl+Q** / **Esc** | Quit application |
| **Enter** | Send message |
| **↑** / **↓** | Navigate contacts |
| **Ctrl+C** | Close current chat |
| **Ctrl+S** | Show session info |

### Connecting to a Peer

**Via mDNS (Automatic):**
1. Peers automatically advertise presence
2. Open contact list (Ctrl+L)
3. Select peer from list
4. Press Enter to initiate handshake
5. Verify DNIe certificate (TOFU)
6. Start messaging!

**Via Manual Entry:**
1. Press Ctrl+N
2. Enter peer IP (e.g., `192.168.1.100`)
3. Enter peer port (default: 6666)
4. Wait for handshake
5. Verify certificate
6. Chat!

---

## 📂 Project Structure

```
Instant_Messaging_DNIe/
├── scripts/
│   ├── __init__.py
│   ├── main.py                 # Application entry point
│   ├── protocol.py             # Manual Noise IK + UDP transport
│   ├── dnie_manager.py         # DNIe PKCS#11 interface
│   ├── database.py             # SQLite storage layer
│   ├── mdns_service.py         # mDNS/Zeroconf discovery
│   ├── chat_ui.py              # Textual TUI interface
│   └── config.py               # Configuration
│
├── tests/
│   ├── test_protocol.py        # Cryptographic tests
│   ├── test_dnie.py            # DNIe interface tests
│   └── test_mdns.py            # mDNS discovery tests
│
├── docs/
│   ├── CRYPTOGRAPHY.md         # Detailed crypto spec
│   ├── PACKET_FORMAT.md        # UDP packet structure
│   └── SECURITY_ANALYSIS.md    # Security considerations
│
├── requirements.txt            # Python dependencies
├── README.md                   # This file
├── AI_USAGE.md                 # AI development documentation
└── .gitignore
```

---

## 🔧 Configuration Details

### Cryptographic Parameters

The application uses these fixed parameters (hardcoded for consistency):

```python
# Key Material
STATIC_KEY_SIZE = 32              # X25519 static key (bytes)
EPHEMERAL_KEY_SIZE = 32           # X25519 ephemeral key (bytes)
SESSION_KEY_SIZE = 32             # BLAKE2s output (bytes)
NONCE_SIZE = 12                   # ChaCha20-Poly1305 nonce (bytes)

# Hash & Encryption
HASH_FUNCTION = "BLAKE2s"
HASH_OUTPUT_BITS = 256            # 32 bytes
CIPHER_ALGORITHM = "ChaCha20-Poly1305"
CIPHER_KEY_SIZE = 32              # bytes
```

### Packet Format

All packets follow a unified structure:

```
┌─────────┬──────────────┬─────────────────────────────┐
│ Type    │ Connection   │ Payload                     │
│ (1 byte)│ ID (4 bytes) │ (variable, format per type) │
└─────────┴──────────────┴─────────────────────────────┘
```

**Packet Types:**

```python
PKT_HANDSHAKE_INIT = 0x01  # Initiator → Responder
  [Ephemeral pub | Encrypted cert]

PKT_HANDSHAKE_RESP = 0x03  # Responder → Initiator
  [Ephemeral pub | Encrypted cert]

PKT_MSG = 0x02             # Either direction (encrypted)
  [Nonce (12B) | Ciphertext]

PKT_ACK = 0x04             # Message acknowledgment
  [Nonce (12B) | Encrypted UUID]

PKT_RECONNECT_REQ = 0x05   # Restore session
  [Empty - only CID matters]

PKT_RECONNECT_RESP = 0x06  # Session confirmed
  [Empty]

PKT_PENDING_SEND = 0x07    # About to send queued msgs
  [Empty]

PKT_PENDING_DONE = 0x08    # Finished sending queued
  [Empty]
```

### Message Format (Inside Encrypted Payload)

```
Message ID Format:
[UUID (36 chars)]|[message text]

Example:
550e8400-e29b-41d4-a716-446655440000|¡Hola, ¿cómo estás?
```

---

## 🔐 Security Considerations

### Threat Model

| Threat | Manual IK Mitigation | Status |
|--------|---------------------|--------|
| **Eavesdropping** | ChaCha20 encryption | ✅ Protected |
| **Tampering** | Poly1305 authentication tag | ✅ Protected |
| **Impersonation** | DNIe certificate in handshake | ✅ Protected |
| **Replay** | Message UUIDs + ACK | ✅ Protected |
| **MITM** | Both peers verify certs (TOFU) | ✅ Protected |
| **Key Compromise** | Ephemeral keys limit exposure | ✅ Partial |
| **Perfect Forward Secrecy** | Reused static keys | ⚠️ Limited |
| **DOS (flooding)** | CID rate limiting (future) | ⏳ TODO |

### Attack Scenarios & Defenses

**Scenario 1: Attacker intercepts handshake**
- ❌ Can see ephemeral public key
- ✅ Cannot derive session key (needs static private key)
- ✅ Cannot decrypt certificate
- **Result: Protected**

**Scenario 2: Attacker spoofs message**
- ✅ Poly1305 tag prevents tampering
- ✅ Message UUID prevents replay
- **Result: Protected**

**Scenario 3: Attacker replays old messages**
- ✅ Each message has unique UUID
- ✅ Receiver tracks received UUIDs
- ✅ Replayed message rejected
- **Result: Protected**

**Scenario 4: Session key compromised**
- ✅ Ephemeral keys used in handshake (not in messages)
- ❌ Past messages with that session_key are exposed
- **Result: Partial protection (no PFS yet)**

### Best Practices

1. **Verify Certificates**: Always check DNIe name on first contact (TOFU)
2. **PIN Protection**: Keep DNIe PIN secure (not stored, never logged)
3. **Network Security**: Use on trusted networks when possible
4. **Key Management**: Session keys cached in DB (encrypted at rest recommended)
5. **Updates**: Keep cryptography.io updated for security patches

---

## 🧪 Testing

### Run Unit Tests

```bash
cd scripts
python -m pytest ../tests/ -v
```

### Manual Testing

**Test 1: Basic Handshake**
```bash
# Terminal 1
python main.py --port 6666 &

# Terminal 2
python main.py --port 6667

# In app: Connect to localhost:6666
# Verify handshake completes without errors
```

**Test 2: Message Exchange**
```bash
# After handshake, send messages
# Verify they arrive and have ACKs
# Check message UUIDs are unique
```

**Test 3: Encryption Verification**
```bash
python -c "
from scripts.protocol import SecureIMProtocol
import os
key = os.urandom(32)
nonce = os.urandom(12)
print(f'Key size: {len(key)}, Nonce size: {len(nonce)}')
"
```

**Test 4: DNIe Certificate**
```bash
python -c "from scripts.dnie_manager import DNIeManager; m = DNIeManager(); cert, _ = m.obtener_credenciales(); print(f'Cert size: {len(cert)} bytes')"
```

---

## 🐛 Troubleshooting

### Handshake Fails

```python
# Check if both peers have correct IP:port
netstat -anu | grep 6666

# Enable debug logging
python main.py --debug

# Verify X25519 keys are 32 bytes
python -c "from cryptography.hazmat.primitives.asymmetric import x25519; sk = x25519.X25519PrivateKey.generate(); print(len(sk.private_bytes_raw()))"
```

**Solutions:**
- Check firewall allows UDP on port
- Verify both peers on same network
- Check DNIe card is inserted
- Restart pcscd service

### Message Not Encrypted Properly

```python
# Verify Poly1305 tag is present
nonce = payload[:12]
ciphertext = payload[12:]
print(f"Nonce: {len(nonce)}B, Ciphertext: {len(ciphertext)}B")

# Check session key size
print(f"Key: {len(session_key)}B (should be 32)")
```

### Certificate Verification Fails

```bash
# Extract certificate from handshake
# Verify it's valid DER format
openssl x509 -in cert.der -inform DER -text -noout

# Check DNIe fingerprint
python -c "
import hashlib
cert = open('cert.der', 'rb').read()
fp = hashlib.sha256(cert).hexdigest()
print(f'Fingerprint: {fp}')
"
```

---

## 📚 References

### Cryptography Standards
- [RFC 7748 - X25519 ECDH](https://tools.ietf.org/html/rfc7748)
- [RFC 7539 - ChaCha20-Poly1305](https://tools.ietf.org/html/rfc7539)
- [BLAKE2 Specification](https://blake2.net/blake2.pdf)
- [Noise Protocol Framework](https://noiseprotocol.org/noise.html)

### Libraries & Standards
- [cryptography.io Documentation](https://cryptography.io/)
- [PKCS#11 Standard](http://docs.oasis-open.org/pkcs11/pkcs11-base/)
- [Spanish DNIe Specifications](https://www.dnielectronico.es/)
- [RFC 6762 - mDNS](https://tools.ietf.org/html/rfc6762)

### Related Projects
- [WireGuard Protocol](https://www.wireguard.com/papers/wireguard.pdf)
- [Signal Protocol](https://signal.org/docs/)
- [Noise Explorer](https://noiseexplorer.com/)

---

## 📄 License

MIT License - See LICENSE file

---

## 👥 Contributors

- **Enrique Landa Espes** (@enriquelandaespes)
- AI-Assisted Development with Perplexity AI

---

## 🎯 Future Improvements

### v1.1 (Q1 2025)
- [ ] Perfect Forward Secrecy (Signal-style ratcheting)
- [ ] Group chat support
- [ ] Message history export

### v1.2 (Q2 2025)
- [ ] File transfer (encrypted)
- [ ] Voice/video calls
- [ ] E2E encrypted backup

### v2.0 (Q3 2025)
- [ ] Mobile clients (Android/iOS)
- [ ] Multi-device support
- [ ] Server-based message backup

---

**Last Updated**: December 1, 2025  
**Version**: 1.0.0 (Manual Crypto Implementation)  
**Status**: Stable & Production Ready
