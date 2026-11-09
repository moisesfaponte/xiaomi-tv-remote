#!/usr/bin/env python3
"""
Mando remoto para Android TV / Google TV / Mi TV desde Linux (Kali).
Usa el mismo protocolo que la app Google TV (puertos 6466/6467).
"""
import argparse
import asyncio
import json
import os
import select
import sys
import termios
import threading
import tty
from pathlib import Path

from androidtvremote2 import (
    AndroidTVRemote,
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
)

from discovery import default_subnet, find_tv, identify
from wol import arp_mac, send_wol

CONFIG_DIR = Path.home() / ".config" / "tvremote"
CONFIG_FILE = CONFIG_DIR / "config.json"
CERT_FILE = CONFIG_DIR / "cert.pem"
KEY_FILE = CONFIG_DIR / "key.pem"

DEFAULT_HOST = os.environ.get("TV_HOST", "")


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG_FILE, 0o600)


def get_host(args) -> str:
    host = args.host or DEFAULT_HOST or load_config().get("host")
    if not host:
        sys.exit(
            "Falta la IP de la TV.\n"
            "  Usa: tvremote.py pair --host <IP de la TV>"
        )
    return host


def make_remote(host: str) -> AndroidTVRemote:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    return AndroidTVRemote(
        client_name="Kali Remote",
        certfile=str(CERT_FILE),
        keyfile=str(KEY_FILE),
        host=host,
    )


def get_subnet(args) -> str:
    return (
        getattr(args, "subnet", None)
        or os.environ.get("TV_SUBNET")
        or load_config().get("subnet")
        or default_subnet()
    )


async def discover_tv(args, known_host: str | None = None) -> dict | None:
    """Busca la TV por MAC/nombre en la subred y guarda la nueva IP."""
    cfg = load_config()
    subnet = get_subnet(args)
    print(f"Buscando la TV en {subnet}...", file=sys.stderr)
    found = await find_tv(
        certfile=str(CERT_FILE),
        keyfile=str(KEY_FILE),
        subnet=subnet,
        known_host=known_host or cfg.get("host"),
        known_name=cfg.get("name"),
        known_mac=cfg.get("mac"),
    )
    if found:
        cfg.update(
            host=found["host"],
            name=found["name"],
            mac=found["mac"],
            subnet=subnet,
        )
        save_config(cfg)
    return found


async def _try_connect(host: str):
    remote = make_remote(host)
    try:
        await remote.async_generate_cert_if_missing()
        await remote.async_connect()
        return remote, None
    except InvalidAuth as exc:
        return None, exc
    except (CannotConnect, ConnectionClosed, OSError) as exc:
        return None, exc


async def open_remote(host: str, args) -> tuple[AndroidTVRemote, str]:
    """Conecta a la TV; si la IP falla, descubre la nueva y reintenta."""
    remote, error = await _try_connect(host)
    if remote is not None:
        return remote, host

    if isinstance(error, InvalidAuth):
        sys.exit("No emparejado. Ejecuta: tv pair --host <IP>")

    found = await discover_tv(args, known_host=host)
    if not found:
        sys.exit(
            f"No se pudo conectar con {host} y no se encontró la TV en la red.\n"
            "Comprueba que está encendida y en la misma Wi-Fi."
        )

    remote, error = await _try_connect(found["host"])
    if remote is None:
        if isinstance(error, InvalidAuth):
            sys.exit("No emparejado. Ejecuta: tv pair --host <IP>")
        sys.exit(f"TV encontrada en {found['host']}, pero no se pudo conectar: {error}")
    return remote, found["host"]


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
async def cmd_pair(args) -> None:
    host = get_host(args)
    remote = make_remote(host)

    generated = await remote.async_generate_cert_if_missing()
    if generated:
        os.chmod(CERT_FILE, 0o600)
        os.chmod(KEY_FILE, 0o600)
        print("Certificado de cliente generado.")

    print(f"Conectando con {host} para emparejar...")
    try:
        await remote.async_start_pairing()
    except CannotConnect:
        sys.exit(
            f"No se pudo conectar a {host}:6467. ¿IP correcta? ¿TV encendida "
            "y en la misma red?"
        )

    print("\n>>> Mira la TV: debería mostrar un código de 6 dígitos. <<<\n")
    code = input("Código: ").strip()

    try:
        await remote.async_finish_pairing(code)
    except InvalidAuth:
        sys.exit("Código incorrecto o emparejamiento rechazado. Inténtalo otra vez.")
    except ConnectionClosed:
        sys.exit("La TV cerró la conexión (¿cancelaste en la TV?).")

    cfg = load_config()
    cfg["host"] = host
    cfg["subnet"] = get_subnet(args)
    save_config(cfg)

    info = await identify(host, str(CERT_FILE), str(KEY_FILE))
    if info:
        cfg["name"], cfg["mac"] = info
        save_config(cfg)
        print(f"TV: {info[0]} ({info[1]})")

    print(f"\n¡Emparejado! IP guardada: {host}")
    print("Prueba:  tv interactive")


async def cmd_key(args) -> None:
    host = get_host(args)
    remote, host = await open_remote(host, args)

    key = args.name.upper()
    if key.startswith("KEYCODE_"):
        key = key[len("KEYCODE_"):]
    remote.send_key_command(key, args.direction.upper())
    await asyncio.sleep(0.3)
    remote.disconnect()
    print(f"Enviado: {key}")


async def cmd_text(args) -> None:
    host = get_host(args)
    remote, host = await open_remote(host, args)
    remote.send_text(args.text)
    await asyncio.sleep(0.3)
    remote.disconnect()
    print(f"Texto enviado: {args.text}")


async def cmd_app(args) -> None:
    host = get_host(args)
    remote, host = await open_remote(host, args)
    remote.send_launch_app_command(args.app)
    await asyncio.sleep(0.3)
    remote.disconnect()
    print(f"App lanzada: {args.app}")


async def cmd_info(args) -> None:
    host = get_host(args)
    remote = make_remote(host)
    await remote.async_generate_cert_if_missing()
    try:
        name, mac = await remote.async_get_name_and_mac()
    except InvalidAuth:
        sys.exit("Aún no está emparejado. Ejecuta: tv pair")
    except (CannotConnect, ConnectionClosed, OSError) as e:
        sys.exit(f"No se pudo conectar con {host}: {e}")
    remote.disconnect()

    cfg = load_config()
    cfg.update(host=host, name=name, mac=mac, subnet=get_subnet(args))
    save_config(cfg)

    print(f"Nombre : {name}")
    print(f"MAC    : {mac}")
    print(f"IP     : {host}")
    print("Identidad guardada para poder reencontrarla si cambia la IP.")


async def cmd_discover(args) -> None:
    found = await discover_tv(args)
    if not found:
        sys.exit(
            f"No se encontró la TV en {get_subnet(args)}.\n"
            "Comprueba que está encendida y en la misma Wi-Fi."
        )
    print("TV encontrada:")
    print(f"  Nombre : {found['name']}")
    print(f"  MAC    : {found['mac']}")
    print(f"  IP     : {found['host']}")
    print("IP actualizada en la configuración.")


async def cmd_wake(args) -> None:
    """Envía un paquete mágico para despertar la TV del standby."""
    host = get_host(args)
    subnet = get_subnet(args)

    try:
        remote, error = await asyncio.wait_for(_try_connect(host), timeout=3.0)
    except asyncio.TimeoutError:
        remote, error = None, None

    if remote is not None:
        remote.disconnect()
        print(f"La TV ya está encendida en {host}.")
        return
    if isinstance(error, InvalidAuth):
        sys.exit("No emparejado. Ejecuta: tv pair --host <IP>")

    cfg = load_config()
    macs = [cfg.get("wol_mac"), arp_mac(host), cfg.get("mac")]
    sent_to = send_wol(macs, host, subnet)
    if not sent_to:
        sys.exit("No hay una MAC válida guardada. Ejecuta: tv info")

    print(f"Paquete mágico enviado desde {len(sent_to)} destinos:")
    for endpoint in sent_to:
        print(f"  {endpoint}")
    print("La TV debería encenderse en unos segundos.")


# --------------------------------------------------------------------------- #
# Modo interactivo
# --------------------------------------------------------------------------- #
HELP = """
┌──────────────────── MANDO TV ────────────────────┐
│  Flechas ........ mover        Enter ........ OK │
│  Retroceso/b .... atrás        h ............ Home│
│  p .............. encender/apagar                │
│  + / - .......... volumen      m ............ mute│
│  Espacio ........ play/pausa   f / w ....... ±10s │
│  n .............. Netflix      y ............ YouTube│
│  s .............. ajustes      i ............ entrada│
│  0-9 ............ números      g ............ guía│
│  t .............. escribir texto                 │
│  q .............. salir                          │
└──────────────────────────────────────────────────┘
"""

# nombre legible -> tecla
SIMPLE_KEYS = {
    "p": "POWER",
    "h": "HOME",
    "b": "BACK",
    "m": "MUTE",
    "s": "SETTINGS",
    "i": "TV_INPUT",
    "g": "GUIDE",
    " ": "MEDIA_PLAY_PAUSE",
    "f": "MEDIA_FAST_FORWARD",
    "w": "MEDIA_REWIND",
    "+": "VOLUME_UP",
    "=": "VOLUME_UP",
    "-": "VOLUME_DOWN",
    "_": "VOLUME_DOWN",
}

ARROWS = {
    "\x1b[A": "DPAD_UP",
    "\x1b[B": "DPAD_DOWN",
    "\x1b[C": "DPAD_RIGHT",
    "\x1b[D": "DPAD_LEFT",
}


def _read_key_blocking(fd: int) -> bytes | None:
    """Lee una pulsación (incluye secuencias de escape de flechas)."""
    try:
        first = os.read(fd, 1)
    except OSError:
        return None
    if not first:
        return None
    if first == b"\x1b":
        # Puede ser una flecha: ESC [ A/B/C/D
        seq = first
        while len(seq) < 4:
            r, _, _ = select.select([fd], [], [], 0.02)
            if not r:
                break
            seq += os.read(fd, 1)
        return seq
    return first


async def cmd_interactive(args) -> None:
    host = get_host(args)
    remote, host = await open_remote(host, args)
    print(f"Conectado a {host}. Esperando teclas... (q para salir)\n")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    stop = threading.Event()

    def reader() -> None:
        tty.setraw(fd)
        text_mode = False
        buffer = ""
        try:
            while not stop.is_set():
                data = _read_key_blocking(fd)
                if data is None:
                    break
                if text_mode:
                    if data in (b"\r", b"\n"):
                        loop.call_soon_threadsafe(queue.put_nowait, ("text", buffer))
                        text_mode = False
                        buffer = ""
                    elif data in (b"\x7f", b"\x08"):
                        buffer = buffer[:-1]
                        loop.call_soon_threadsafe(
                            queue.put_nowait, ("echo", "\b \b")
                        )
                    elif data == b"\x1b":
                        text_mode = False
                        buffer = ""
                        loop.call_soon_threadsafe(queue.put_nowait, ("echo", "\n"))
                    else:
                        try:
                            char = data.decode()
                        except UnicodeDecodeError:
                            continue
                        buffer += char
                        loop.call_soon_threadsafe(queue.put_nowait, ("echo", char))
                    continue

                if data == b"t":
                    text_mode = True
                    buffer = ""
                    loop.call_soon_threadsafe(
                        queue.put_nowait, ("prompt", "\nTexto: ")
                    )
                    continue
                loop.call_soon_threadsafe(queue.put_nowait, ("key", data))
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    print(HELP)

    try:
        while True:
            kind, value = await queue.get()
            if kind == "echo":
                sys.stdout.write(value)
                sys.stdout.flush()
                continue
            if kind == "prompt":
                sys.stdout.write(value)
                sys.stdout.flush()
                continue
            if kind == "text":
                if value:
                    remote.send_text(value)
                sys.stdout.write("\n")
                sys.stdout.flush()
                continue

            data = value
            if data == b"q":
                break
            if data in (b"\r", b"\n"):
                remote.send_key_command("DPAD_CENTER")
                continue
            if data in (b"\x7f", b"\x08", b"\x1b"):
                remote.send_key_command("BACK")
                continue
            if data in (b"\t",):
                remote.send_key_command("TAB")
                continue

            text = data.decode("latin-1", "ignore")
            if text in ARROWS:
                remote.send_key_command(ARROWS[text])
                continue
            if text in SIMPLE_KEYS:
                remote.send_key_command(SIMPLE_KEYS[text])
                continue
            if text.isdigit():
                remote.send_key_command(text)
                continue
            if text in ("n", "y"):
                app = (
                    "https://www.netflix.com/title"
                    if text == "n"
                    else "https://www.youtube.com"
                )
                remote.send_launch_app_command(app)
                continue
    except (ConnectionClosed, OSError):
        print("\nSe perdió la conexión con la TV.")
    finally:
        stop.set()
        remote.disconnect()
        print("\nMando cerrado.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mando remoto para Android TV / Google TV / Mi TV"
    )
    parser.add_argument("--host", help="IP de la TV (se guarda al emparejar)")
    parser.add_argument(
        "--subnet",
        help=f"Subred para buscar la TV si cambia de IP (por defecto {default_subnet()})",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("pair", help="Emparejar este PC con la TV")
    sub.add_parser("interactive", help="Abrir el mando interactivo (por defecto)")
    sub.add_parser("discover", help="Buscar la TV en la red y actualizar su IP")
    sub.add_parser("wake", help="Encender la TV con Wake-on-LAN (desde standby)")

    p_key = sub.add_parser("key", help="Enviar una tecla, p.ej. POWER o DPAD_UP")
    p_key.add_argument("name")
    p_key.add_argument(
        "--direction", default="SHORT", choices=["SHORT", "START_LONG", "END_LONG"]
    )

    p_text = sub.add_parser("text", help="Escribir texto en un campo de la TV")
    p_text.add_argument("text")

    p_app = sub.add_parser("app", help="Abrir una app (id o URL)")
    p_app.add_argument("app")

    sub.add_parser("info", help="Ver nombre y MAC de la TV")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        args.command = "interactive"

    try:
        asyncio.run(
            {
                "pair": cmd_pair,
                "key": cmd_key,
                "text": cmd_text,
                "app": cmd_app,
                "info": cmd_info,
                "discover": cmd_discover,
                "wake": cmd_wake,
                "interactive": cmd_interactive,
            }[args.command](args)
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
