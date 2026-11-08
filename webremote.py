#!/usr/bin/env python3
"""
Mando web para Android TV / Google TV / Mi TV.
Sirve una interfaz con rueda direccional y botones, y habla con la TV
por el protocolo de Google TV (androidtvremote2).

Si la IP guardada deja de responder, escanea la subred, identifica la TV por
su MAC/nombre y actualiza la IP automáticamente.
"""
import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path

from aiohttp import web
from androidtvremote2 import (
    AndroidTVRemote,
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
)

from discovery import default_subnet, find_tv
from tvremote import CERT_FILE, KEY_FILE, load_config, save_config

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "remote.html"
MANIFEST_FILE = BASE_DIR / "manifest.webmanifest"
SW_FILE = BASE_DIR / "sw.js"
ICON_DIR = BASE_DIR / "icons"

MAX_TEXT = 200
CONNECT_TIMEOUT = 8.0
MIN_SCAN_INTERVAL = 8.0
RETRY_FIRST_DELAY = 5.0
RETRY_MAX_DELAY = 30.0
WATCH_INTERVAL = 1.0
RESUME_GAP = 10.0


def boot_time() -> float:
    """Reloj que sigue avanzando durante la suspensión del equipo (Linux)."""
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


class TvController:
    """Conexión persistente con la TV + redescubrimiento automático."""

    def __init__(self, host: str, subnet: str) -> None:
        self.host = host
        self.subnet = subnet

        cfg = load_config()
        self.known_name: str | None = cfg.get("name")
        self.known_mac: str | None = cfg.get("mac")

        self.state: dict = {
            "host": host,
            "subnet": subnet,
            "connected": False,
            "is_on": None,
            "current_app": None,
            "volume": None,
            "error": None,
            "scanning": False,
            "last_scan": None,
        }

        self.remote: AndroidTVRemote | None = None
        self._lock: asyncio.Lock | None = None
        self._scan_lock: asyncio.Lock | None = None
        self._retry_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._last_scan = 0.0
        self._last_beat = boot_time()

    # ------------------------------------------------------------ conexión --
    def _attach(self, remote: AndroidTVRemote) -> None:
        def on_available(value: bool) -> None:
            self.state["connected"] = value
            if value:
                self.state["error"] = None
            elif self.remote is not None:
                # Se cayó la conexión (o cambió la IP): reintentar.
                self._start_rediscovery()

        remote.add_is_available_updated_callback(on_available)
        remote.add_is_on_updated_callback(
            lambda value: self.state.__setitem__("is_on", value)
        )
        remote.add_current_app_updated_callback(
            lambda value: self.state.__setitem__("current_app", value)
        )
        remote.add_volume_info_updated_callback(
            lambda info: self.state.__setitem__("volume", dict(info))
        )

    def _save_host(self, host: str) -> None:
        cfg = load_config()
        cfg.update(
            host=host,
            name=self.known_name,
            mac=self.known_mac,
            subnet=self.subnet,
        )
        save_config(cfg)

    async def _connect(self, host: str) -> AndroidTVRemote:
        """Crea y conecta un cliente. Debe llamarse con self._lock tomado."""
        remote = AndroidTVRemote(
            client_name="Kali Web Remote",
            certfile=str(CERT_FILE),
            keyfile=str(KEY_FILE),
            host=host,
        )
        await remote.async_generate_cert_if_missing()
        self._attach(remote)
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=CONNECT_TIMEOUT)
        except Exception:
            remote.disconnect()
            raise

        self.remote = remote
        self.host = host
        self.state["host"] = host
        self.state["connected"] = True
        self.state["error"] = None
        self._save_host(host)
        return remote

    def _drop(self) -> None:
        remote, self.remote = self.remote, None
        self.state["connected"] = False
        if remote is not None:
            try:
                remote.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------- suspensión --
    def check_resume(self) -> bool:
        """Si el equipo estuvo suspendido, tira la conexión vieja y reconecta."""
        now = boot_time()
        gap = now - self._last_beat
        if gap <= RESUME_GAP:
            return False

        self._last_beat = now
        print(f"[tv] equipo reanudado tras {gap:.0f}s suspendido; reconectando")
        self.state["connected"] = False
        self.state["error"] = "reconectando tras suspensión…"
        self._drop()
        self._start_rediscovery()
        return True

    async def _suspend_watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCH_INTERVAL)
            if not self.check_resume():
                self._last_beat = boot_time()

    # ---------------------------------------------------------- descubrir --
    async def _discover_once(self, force: bool = False) -> dict | None:
        now = time.monotonic()
        if not force and now - self._last_scan < MIN_SCAN_INTERVAL:
            return None
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()

        async with self._scan_lock:
            now = time.monotonic()
            if not force and now - self._last_scan < MIN_SCAN_INTERVAL:
                return None

            self.state["scanning"] = True
            self.state["error"] = "buscando TV…"
            print(f"[tv] escaneando {self.subnet}…")
            try:
                found = await find_tv(
                    certfile=str(CERT_FILE),
                    keyfile=str(KEY_FILE),
                    subnet=self.subnet,
                    known_host=self.host,
                    known_name=self.known_name,
                    known_mac=self.known_mac,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[tv] error al escanear: {exc}")
                found = None
            finally:
                self._last_scan = time.monotonic()
                self.state["last_scan"] = self._last_scan
                self.state["scanning"] = False

            if found:
                self.host = found["host"]
                self.known_name = found["name"]
                self.known_mac = found["mac"]
                self.state["host"] = found["host"]
                self.state["error"] = None
                self._save_host(found["host"])
                print(f"[tv] encontrada en {found['host']} ({found['name']})")
            return found

    def _start_rediscovery(self) -> None:
        if self._retry_task is not None and not self._retry_task.done():
            return
        self._retry_task = asyncio.create_task(self._rediscovery_loop())

    async def _rediscovery_loop(self) -> None:
        """Reintenta hasta encontrar y conectar la TV. Se detiene al lograrlo."""
        delay = RETRY_FIRST_DELAY
        try:
            while True:
                if self.remote is not None and self.state["connected"]:
                    return

                found = await self._discover_once()
                if found:
                    if self._lock is None:
                        self._lock = asyncio.Lock()
                    async with self._lock:
                        if self.remote is not None and self.state["connected"]:
                            return
                        if self.remote is not None:
                            self._drop()
                        try:
                            await self._connect(found["host"])
                            print(f"[tv] conectada en {found['host']}")
                            self.state["scanning"] = False
                            return
                        except InvalidAuth:
                            self.state["error"] = "no emparejado"
                            self.state["scanning"] = False
                            return
                        except Exception as exc:  # noqa: BLE001
                            self.state["error"] = (
                                f"TV en {found['host']} no conecta: {exc}"
                            )

                if not self.state["scanning"]:
                    self.state["error"] = "TV no encontrada; reintentando…"
                await asyncio.sleep(delay)
                delay = min(delay * 1.6, RETRY_MAX_DELAY)
        finally:
            self.state["scanning"] = False

    async def force_discover(self) -> dict | None:
        """Búsqueda manual (botón “Buscar TV”). Ignora el límite de frecuencia."""
        found = await self._discover_once(force=True)
        if not found:
            return None

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.remote is not None:
                self._drop()
            try:
                await self._connect(found["host"])
            except InvalidAuth:
                self.state["error"] = "no emparejado"
                raise
            except Exception as exc:  # noqa: BLE001
                self.state["error"] = f"encontrada en {found['host']}, no conecta: {exc}"
                self._start_rediscovery()
                raise
        return found

    # ------------------------------------------------------------ arranque --
    async def start(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._last_beat = boot_time()
            self._watch_task = asyncio.create_task(self._suspend_watchdog())
        try:
            await self._ensure()
            print(f"[tv] conectado a {self.host}")
        except InvalidAuth:
            self.state["error"] = "no emparejado"
            print("[tv] ERROR: no emparejado. Ejecuta: ./tv pair")
        except Exception as exc:  # noqa: BLE001
            if not self.state["error"]:
                self.state["error"] = str(exc) or exc.__class__.__name__
            print(f"[tv] no se pudo conectar: {exc}")

    # ------------------------------------------------------------ comandos --
    async def _ensure(self) -> AndroidTVRemote:
        self.check_resume()
        if self.remote is not None and self.state["connected"]:
            return self.remote
        if self.remote is not None:
            self._drop()

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.remote is not None and self.state["connected"]:
                return self.remote

            try:
                return await self._connect(self.host)
            except InvalidAuth:
                self.state["error"] = "no emparejado"
                raise
            except (CannotConnect, ConnectionClosed, OSError, asyncio.TimeoutError):
                pass

            found = await self._discover_once()
            if found:
                try:
                    return await self._connect(found["host"])
                except InvalidAuth:
                    self.state["error"] = "no emparejado"
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.state["error"] = (
                        f"encontrada en {found['host']}, no conecta: {exc}"
                    )
                    self._start_rediscovery()
                    raise

            self.state["error"] = "TV no encontrada; reintentando…"
            self._start_rediscovery()
            raise CannotConnect(f"No se encontró la TV en {self.subnet}")

    async def _run(self, action) -> None:
        """Ejecuta una acción con un reintento si la conexión se cayó."""
        last_error: Exception | None = None
        for _ in range(2):
            remote = await self._ensure()
            try:
                action(remote)
                return
            except ConnectionClosed as exc:
                last_error = exc
                self._drop()
                await asyncio.sleep(0.25)
        assert last_error is not None
        raise last_error

    async def send_key(self, key: str, direction: str = "SHORT") -> None:
        await self._run(lambda remote: remote.send_key_command(key, direction))

    async def send_text(self, text: str) -> None:
        await self._run(lambda remote: remote.send_text(text))

    async def launch_app(self, app: str) -> None:
        await self._run(lambda remote: remote.send_launch_app_command(app))


# --------------------------------------------------------------------- API --
def json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def index(_request: web.Request) -> web.Response:
    return web.Response(
        text=HTML_FILE.read_text(encoding="utf-8"), content_type="text/html"
    )


async def api_status(request: web.Request) -> web.Response:
    controller: TvController = request.app["controller"]
    controller.check_resume()
    return web.json_response({"ok": True, **controller.state})


async def api_key(request: web.Request) -> web.Response:
    controller: TvController = request.app["controller"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return json_error("JSON inválido")

    key = str(data.get("key", "")).strip().upper()
    direction = str(data.get("direction", "SHORT")).upper()
    if not key or not key.replace("_", "").isalnum() or len(key) > 40:
        return json_error("Tecla inválida")
    if direction not in ("SHORT", "START_LONG", "END_LONG"):
        return json_error("Dirección inválida")

    try:
        await controller.send_key(key, direction)
    except InvalidAuth:
        return json_error("No emparejado. Ejecuta ./tv pair en el PC.", 409)
    except (CannotConnect, ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
        return json_error(f"TV no disponible: {exc}", 502)
    except ValueError:
        return json_error(f"Tecla desconocida: {key}")
    return web.json_response({"ok": True, "key": key})


async def api_text(request: web.Request) -> web.Response:
    controller: TvController = request.app["controller"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return json_error("JSON inválido")

    text = str(data.get("text", ""))
    if not text or len(text) > MAX_TEXT:
        return json_error(f"Texto vacío o demasiado largo (máx. {MAX_TEXT})")

    try:
        await controller.send_text(text)
    except InvalidAuth:
        return json_error("No emparejado. Ejecuta ./tv pair en el PC.", 409)
    except (CannotConnect, ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
        return json_error(f"TV no disponible: {exc}", 502)
    return web.json_response({"ok": True})


async def api_app(request: web.Request) -> web.Response:
    controller: TvController = request.app["controller"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return json_error("JSON inválido")

    app = str(data.get("app", "")).strip()
    if not app.startswith(("http://", "https://")) or len(app) > 300:
        return json_error("URL de app inválida")

    try:
        await controller.launch_app(app)
    except InvalidAuth:
        return json_error("No emparejado. Ejecuta ./tv pair en el PC.", 409)
    except (CannotConnect, ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
        return json_error(f"TV no disponible: {exc}", 502)
    return web.json_response({"ok": True})


async def api_discover(request: web.Request) -> web.Response:
    controller: TvController = request.app["controller"]
    try:
        found = await controller.force_discover()
    except InvalidAuth:
        return json_error("No emparejado. Ejecuta ./tv pair en el PC.", 409)
    except (CannotConnect, ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
        return json_error(f"TV encontrada pero no conecta: {exc}", 502)

    if not found:
        return json_error(
            f"No se encontró la TV en {controller.subnet}. "
            "Comprueba que está encendida y en la misma Wi-Fi.",
            404,
        )
    return web.json_response({"ok": True, **found})


# ---------------------------------------------------------------- estáticos --
async def manifest(_request: web.Request) -> web.Response:
    return web.Response(
        text=MANIFEST_FILE.read_text(encoding="utf-8"),
        content_type="application/manifest+json",
    )


async def service_worker(_request: web.Request) -> web.Response:
    return web.Response(
        text=SW_FILE.read_text(encoding="utf-8"),
        content_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


async def icon(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    path = ICON_DIR / name
    if not path.is_file() or path.parent != ICON_DIR:
        raise web.HTTPNotFound()
    return web.FileResponse(path)


# ---------------------------------------------------------------- servidor --
def lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


async def on_startup(app: web.Application) -> None:
    app["start_task"] = asyncio.create_task(app["controller"].start())


async def on_cleanup(app: web.Application) -> None:
    task = app.get("start_task")
    if task and not task.done():
        task.cancel()
    controller: TvController = app["controller"]
    if controller._retry_task and not controller._retry_task.done():
        controller._retry_task.cancel()
    if controller._watch_task and not controller._watch_task.done():
        controller._watch_task.cancel()
    controller._drop()


def build_app(controller: TvController) -> web.Application:
    app = web.Application()
    app["controller"] = controller
    app.router.add_get("/", index)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/icons/{name}", icon)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/key", api_key)
    app.router.add_post("/api/text", api_text)
    app.router.add_post("/api/app", api_app)
    app.router.add_post("/api/discover", api_discover)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    cfg = load_config()
    fallback_subnet = default_subnet()
    parser = argparse.ArgumentParser(description="Mando web para Mi TV / Android TV")
    parser.add_argument("--host", help="IP de la TV")
    parser.add_argument("--port", type=int, default=8765, help="Puerto web (8765)")
    parser.add_argument(
        "--subnet",
        default=cfg.get("subnet") or fallback_subnet,
        help=f"Subred para buscar la TV si cambia de IP (por defecto {fallback_subnet})",
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Interfaz de escucha (0.0.0.0 = toda la LAN, 127.0.0.1 = solo este PC)",
    )
    parser.add_argument("--open", action="store_true", help="Abrir el navegador")
    args = parser.parse_args()

    host = args.host or cfg.get("host")
    if not host:
        sys.exit("Falta la IP de la TV. Usa: ./tvweb --host <IP de la TV>")

    cfg["host"] = host
    cfg["subnet"] = args.subnet
    save_config(cfg)

    controller = TvController(host, args.subnet)
    app = build_app(controller)

    url = f"http://{lan_ip()}:{args.port}/"
    print(f"\n  Mando web listo:  {url}")
    print(f"  En este PC:       http://127.0.0.1:{args.port}/")
    print(f"  TV guardada:      {host}  (subred {args.subnet})")
    print("  Ctrl+C para parar.\n")

    if args.open:
        import webbrowser

        webbrowser.open(url)

    web.run_app(app, host=args.bind, port=args.port, print=None)


if __name__ == "__main__":
    main()
