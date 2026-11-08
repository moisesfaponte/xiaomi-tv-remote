"""Descubrimiento de la TV en la red local.

La TV se identifica por su MAC (y nombre), no por su IP. Si la IP guardada
deja de responder, se escanea la subred buscando hosts con los puertos del
mando abiertos y se verifica la identidad contra el certificado de la TV.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from androidtvremote2 import AndroidTVRemote, CannotConnect

DEFAULT_SUBNET = "192.168.1.0/24"
REMOTE_PORT = 6466
PAIR_PORT = 6467
MAX_CANDIDATES = 10


def normalize_mac(mac: str | None) -> str:
    if not mac:
        return ""
    return mac.strip().upper().replace("-", ":")


def local_ip() -> str:
    """IP de este equipo en la interfaz por la que sale el tráfico."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def default_subnet(prefix: int = 24) -> str:
    """Subred local deducida de la IP de este equipo (p. ej. 192.168.1.0/24)."""
    ip = local_ip()
    if ip.startswith("127."):
        return DEFAULT_SUBNET
    try:
        return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
    except ValueError:
        return DEFAULT_SUBNET


async def port_open(ip: str, port: int = REMOTE_PORT, timeout: float = 0.4) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError):
        pass
    return True


async def scan_subnet(
    subnet: str = DEFAULT_SUBNET, timeout: float = 0.4, concurrency: int = 128
) -> list[str]:
    """Devuelve las IPs de la subred con el puerto del mando abierto."""
    network = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(host) for host in network.hosts()]
    semaphore = asyncio.Semaphore(concurrency)

    async def check(ip: str) -> str | None:
        async with semaphore:
            return ip if await port_open(ip, REMOTE_PORT, timeout) else None

    results = await asyncio.gather(*(check(ip) for ip in hosts))
    return [ip for ip in results if ip]


async def identify(
    ip: str, certfile: str, keyfile: str, timeout: float = 5.0
) -> tuple[str, str] | None:
    """Nombre y MAC que la TV presenta en su certificado (sin emparejar)."""
    remote = AndroidTVRemote(
        client_name="Kali Discover",
        certfile=certfile,
        keyfile=keyfile,
        host=ip,
    )
    try:
        return await asyncio.wait_for(remote.async_get_name_and_mac(), timeout=timeout)
    except (CannotConnect, OSError, asyncio.TimeoutError):
        return None
    finally:
        remote.disconnect()


def matches(
    name: str, mac: str, known_name: str | None, known_mac: str | None
) -> bool:
    if known_mac:
        return normalize_mac(mac) == normalize_mac(known_mac)
    if known_name:
        return name.strip().lower() == known_name.strip().lower()
    return True


async def find_tv(
    *,
    certfile: str,
    keyfile: str,
    subnet: str | None = None,
    known_host: str | None = None,
    known_name: str | None = None,
    known_mac: str | None = None,
    scan_timeout: float = 0.4,
) -> dict | None:
    """Busca la TV. Primero prueba la IP guardada y luego escanea la subred."""
    subnet = subnet or default_subnet()
    candidates: list[str] = []

    if known_host and await port_open(known_host, REMOTE_PORT, scan_timeout):
        candidates.append(known_host)

    for ip in await scan_subnet(subnet, timeout=scan_timeout):
        if ip not in candidates:
            candidates.append(ip)

    for ip in candidates[:MAX_CANDIDATES]:
        info = await identify(ip, certfile, keyfile)
        if not info:
            continue
        name, mac = info
        if matches(name, mac, known_name, known_mac):
            return {"host": ip, "name": name, "mac": mac}

    return None
