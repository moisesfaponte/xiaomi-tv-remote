"""Wake-on-LAN para la TV: construcción y envío del paquete mágico.

Solo usa la librería estándar. El paquete se envía por UDP a la dirección de
broadcast de la subred (y a la IP de la TV como respaldo), que es lo que
necesita la tarjeta de red de la TV para despertar desde standby.
"""

from __future__ import annotations

import ipaddress
import socket

WOL_PORTS = (9, 7)
WOL_REPEAT = 3


def normalize_mac(mac: str | None) -> str | None:
    """Devuelve la MAC en formato AA:BB:CC:DD:EE:FF, o None si no es válida."""
    if not mac:
        return None
    cleaned = "".join(ch for ch in mac if ch.isalnum())
    if len(cleaned) != 12:
        return None
    try:
        bytes.fromhex(cleaned)
    except ValueError:
        return None
    pairs = [cleaned[i : i + 2] for i in range(0, 12, 2)]
    return ":".join(pairs).upper()


def magic_packet(mac: str) -> bytes:
    """6 bytes 0xFF seguidos de la MAC repetida 16 veces."""
    normalized = normalize_mac(mac)
    if normalized is None:
        raise ValueError(f"MAC inválida: {mac}")
    raw = bytes.fromhex(normalized.replace(":", ""))
    return b"\xff" * 6 + raw * 16


def arp_mac(host: str) -> str | None:
    """MAC que la red tiene asociada a esa IP (tabla ARP de Linux)."""
    try:
        with open("/proc/net/arp", encoding="ascii") as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == host:
                    mac = normalize_mac(parts[3])
                    if mac and mac != "00:00:00:00:00:00":
                        return mac
    except OSError:
        pass
    return None


def broadcast_targets(host: str, subnet: str | None) -> list[str]:
    """Direcciones a las que enviar: broadcast de subred, global y la TV."""
    targets: list[str] = []
    if subnet:
        try:
            network = ipaddress.ip_network(subnet, strict=False)
            targets.append(str(network.broadcast_address))
        except ValueError:
            pass
    targets.extend([host, "255.255.255.255"])
    return list(dict.fromkeys(targets))


def send_wol(
    macs: list[str | None],
    host: str,
    subnet: str | None = None,
    ports: tuple[int, ...] = WOL_PORTS,
    repeat: int = WOL_REPEAT,
) -> list[str]:
    """Envía el paquete mágico a todas las MAC válidas.

    Devuelve la lista de destinos usados (para el log).
    """
    valid: list[str] = []
    for mac in macs:
        normalized = normalize_mac(mac)
        if normalized and normalized not in valid:
            valid.append(normalized)
    if not valid:
        return []

    packets = [magic_packet(mac) for mac in valid]
    endpoints = [
        (target, port)
        for target in broadcast_targets(host, subnet)
        for port in ports
    ]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for _ in range(max(1, repeat)):
            for target, port in endpoints:
                for packet in packets:
                    try:
                        sock.sendto(packet, (target, port))
                    except OSError:
                        pass
    finally:
        sock.close()

    return [f"{target}:{port}" for target, port in endpoints]
