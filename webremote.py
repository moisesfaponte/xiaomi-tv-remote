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
import math
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

from discovery import REMOTE_PORT, default_subnet, find_tv, port_open
from tvremote import CERT_FILE, KEY_FILE, load_config, save_config
from wol import arp_mac, normalize_mac, send_wol

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "remote.html"
MANIFEST_FILE = BASE_DIR / "manifest.webmanifest"
SW_FILE = BASE_DIR / "sw.js"
ICON_DIR = BASE_DIR / "icons"

MAX_TEXT = 200
CONNECT_TIMEOUT = 8.0
QUICK_CONNECT_TIMEOUT = 2.5
MIN_SCAN_INTERVAL = 8.0
RETRY_FIRST_DELAY = 5.0
RETRY_MAX_DELAY = 30.0
WATCH_INTERVAL = 1.0
RESUME_GAP = 10.0
WAKE_TIMEOUT = 45.0
WAKE_POLL_INTERVAL = 2.0
POWER_COOLDOWN = 2.0
BOOT_TIMEOUT = 12.0
EVENT_HEARTBEAT = 15.0
MAX_SLEEP_MINUTES = 720


class PowerBusy(Exception):
    """Ya se está encendiendo o apagando la TV."""


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
        self.wol_mac: str | None = normalize_mac(cfg.get("wol_mac")) or arp_mac(host)

        self.state: dict = {
            "host": host,
            "subnet": subnet,
            "connected": False,
            "is_on": None,
            "current_app": None,
            "volume": None,
            "error": None,
            "scanning": False,
            "waking": False,
            "wol_mac": self.wol_mac,
            "last_scan": None,
            "sleep_until": None,
        }

        self.remote: AndroidTVRemote | None = None
        self._lock: asyncio.Lock | None = None
        self._scan_lock: asyncio.Lock | None = None
        self._power_lock: asyncio.Lock | None = None
        self._retry_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._sleep_task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._last_payload: str | None = None
        self._last_scan = 0.0
        self._last_power = 0.0
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

    # --------------------------------------------------- estado en vivo --
    def status_payload(self) -> dict:
        """Estado para la web, con la cuenta atrás del temporizador ya resuelta."""
        payload = dict(self.state)
        until = payload.get("sleep_until")
        payload["sleep_remaining"] = (
            max(0, round(until - time.time())) if until is not None else None
        )
        return payload

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _pump_state(self) -> None:
        """Envía el estado a las webs conectadas solo si ha cambiado."""
        if not self._subscribers:
            return
        payload = json.dumps(self.status_payload())
        if payload == self._last_payload:
            return
        self._last_payload = payload
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------ temporizador --
    def cancel_sleep(self, reason: str = "") -> bool:
        """Cancela el temporizador sin enviar nada a la TV."""
        task, self._sleep_task = self._sleep_task, None
        had_timer = self.state.get("sleep_until") is not None
        self.state["sleep_until"] = None
        if task is not None and not task.done():
            task.cancel()
        if had_timer and reason:
            print(f"[tv] temporizador cancelado ({reason})")
        return had_timer

    async def set_sleep(self, minutes: float | None) -> dict:
        """Programa el apagado (minutos) o lo cancela con None/0."""
        self.cancel_sleep()
        if not minutes or minutes <= 0:
            return {"sleep_until": None}

        minutes = min(float(minutes), MAX_SLEEP_MINUTES)
        self.state["sleep_until"] = time.time() + minutes * 60
        self._sleep_task = asyncio.create_task(self._sleep_worker())
        pretty = int(minutes) if minutes == int(minutes) else minutes
        print(f"[tv] temporizador: apagado en {pretty} min")
        return {
            "sleep_until": self.state["sleep_until"],
            "sleep_remaining": int(minutes * 60),
        }

    async def _sleep_worker(self) -> None:
        try:
            while True:
                until = self.state.get("sleep_until")
                if until is None:
                    return
                remaining = until - time.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, 5.0))

            if self.state.get("is_on") is True:
                try:
                    await self.send_key("POWER")
                    print("[tv] temporizador: TV apagada")
                except Exception as exc:  # noqa: BLE001
                    print(f"[tv] temporizador: no se pudo apagar: {exc}")
            else:
                # Si ya está apagada, un POWER la encendería.
                print("[tv] temporizador: la TV no está encendida; no se envía POWER")
        finally:
            self.state["sleep_until"] = None
            if self._sleep_task is asyncio.current_task():
                self._sleep_task = None

    def _save_host(self, host: str) -> None:
        cfg = load_config()
        cfg.update(
            host=host,
            name=self.known_name,
            mac=self.known_mac,
            wol_mac=self.wol_mac,
            subnet=self.subnet,
        )
        save_config(cfg)

    def _remember_wol_mac(self, host: str) -> None:
        """Guarda la MAC de la interfaz de red (Ethernet si la TV va por cable)."""
        mac = arp_mac(host)
        if not mac or mac == self.wol_mac:
            return
        self.wol_mac = mac
        self.state["wol_mac"] = mac
        cfg = load_config()
        cfg["wol_mac"] = mac
        save_config(cfg)
        print(f"[tv] MAC para Wake-on-LAN: {mac}")

    async def _connect(self, host: str, timeout: float = CONNECT_TIMEOUT) -> AndroidTVRemote:
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
            await asyncio.wait_for(remote.async_connect(), timeout=timeout)
        except Exception:
            remote.disconnect()
            raise

        self.remote = remote
        self.host = host
        self.state["host"] = host
        self.state["connected"] = True
        self.state["error"] = None
        self._save_host(host)
        self._remember_wol_mac(host)
        return remote

    def _drop(self) -> None:
        remote, self.remote = self.remote, None
        self.state["connected"] = False
        if remote is not None:
            try:
                remote.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------ encender/apagar --
    def _wol_macs(self) -> list[str | None]:
        """MACs candidatas: ARP (Ethernet), la guardada y la del certificado."""
        return [self.wol_mac, arp_mac(self.host), self.known_mac]

    async def _try_connect_quiet(self, host: str | None = None) -> bool:
        """Intenta conectar sin lanzar redescubrimiento ni errores visibles."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.remote is not None and self.state["connected"]:
                return True
            if self.remote is not None:
                self._drop()
            try:
                await self._connect(host or self.host, timeout=QUICK_CONNECT_TIMEOUT)
                return True
            except InvalidAuth:
                self.state["error"] = "no emparejado"
                raise
            except Exception:  # noqa: BLE001
                return False

    async def _finish_wake(self) -> bool:
        """Tras el WoL: si la TV sigue en standby, un POWER la enciende."""
        for _ in range(6):
            if self.state["is_on"] is not None:
                break
            await asyncio.sleep(0.5)
        if self.state["is_on"] is False:
            try:
                await self.send_key("POWER")
            except Exception:  # noqa: BLE001
                pass
        print(f"[tv] TV disponible en {self.host}")
        return True

    async def _wait_on(self, timeout: float) -> bool:
        """Espera a que la TV confirme que está encendida (is_on == True)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state["is_on"] is True:
                return True
            await asyncio.sleep(0.5)
        return self.state["is_on"] is True

    async def _wait_connected(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._try_connect_quiet():
                return await self._finish_wake()

            found = await self._discover_once()
            if found and await self._try_connect_quiet(found["host"]):
                return await self._finish_wake()
            await asyncio.sleep(WAKE_POLL_INTERVAL)
        return False

    async def power_toggle(self) -> dict:
        """Un solo botón: apaga si está encendida, enciende si está en standby."""
        if self._power_lock is None:
            self._power_lock = asyncio.Lock()
        if self._power_lock.locked():
            raise PowerBusy("La TV se está encendiendo o apagando")

        async with self._power_lock:
            now = time.monotonic()
            if now - self._last_power < POWER_COOLDOWN:
                raise PowerBusy("Espera un momento antes de volver a pulsar")
            self._last_power = now
            self.check_resume()

            # 1) Dice estar conectada: comprobar que la TV sigue en red.
            #    Al apagarla, la conexión puede seguir "viva" un rato aunque
            #    la TV ya haya cerrado todos los puertos.
            if self.remote is not None and self.state["connected"]:
                if await port_open(self.host, REMOTE_PORT, timeout=0.6):
                    try:
                        await self.send_key("POWER")
                        print("[tv] POWER enviado (encendida -> standby)")
                        self.cancel_sleep("botón Power")
                        return {"action": "sent", "state": "off"}
                    except (
                        CannotConnect,
                        ConnectionClosed,
                        OSError,
                        asyncio.TimeoutError,
                    ):
                        self._drop()
                else:
                    print("[tv] la TV ya no está en red; se asume apagada")
                    self._drop()

            # 2) Misma IP: encendida, o standby con la red todavía viva.
            if await self._try_connect_quiet():
                await self.send_key("POWER")
                print("[tv] POWER enviado (standby -> encendida)")
                self.cancel_sleep("botón Power")
                return {"action": "sent", "state": "on"}

            # 3) No responde: la damos por apagada y enviamos Wake-on-LAN.
            self.state["waking"] = True
            self.state["error"] = None
            try:
                sent_to = await asyncio.to_thread(
                    send_wol, self._wol_macs(), self.host, self.subnet
                )
                print(
                    "[tv] Wake-on-LAN enviado"
                    + (f" a {', '.join(sent_to)}" if sent_to else " (sin MAC válida)")
                )
                if await self._wait_connected(WAKE_TIMEOUT):
                    self.cancel_sleep("botón Power")
                    return {"action": "woke", "state": "on"}
                self.state["error"] = (
                    "no se pudo encender la TV; comprueba que no esté desenchufada"
                )
                raise CannotConnect("no se pudo encender la TV")
            finally:
                self.state["waking"] = False

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
            self._pump_state()

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

    async def _wake_from_standby(self) -> str:
        """Con la TV conectada: si sigue en standby, un POWER la enciende."""
        for _ in range(6):
            if self.state["is_on"] is not None:
                break
            await asyncio.sleep(0.5)
        if self.state["is_on"] is False:
            self.state["waking"] = True
            try:
                await self.send_key("POWER")
                await self._wait_on(BOOT_TIMEOUT)
            finally:
                self.state["waking"] = False
            return "sent"
        return "launched"

    async def launch_app_waking(self, app: str) -> dict:
        """Abre una app encendiendo antes la TV si hace falta (un solo toque)."""
        if self._power_lock is None:
            self._power_lock = asyncio.Lock()
        if self._power_lock.locked():
            raise PowerBusy("La TV se está encendiendo o apagando")

        async with self._power_lock:
            self.check_resume()
            action = "launched"
            ready = False

            # La conexión puede seguir "viva" un rato tras apagar la TV:
            # comprobar el puerto antes de fiarse de ella.
            if self.remote is not None and self.state["connected"]:
                if await port_open(self.host, REMOTE_PORT, timeout=0.6):
                    ready = True
                else:
                    print("[tv] conexión de standby caducada; se descarta")
                    self._drop()

            if ready or await self._try_connect_quiet():
                action = await self._wake_from_standby()
            else:
                self.state["waking"] = True
                self.state["error"] = None
                try:
                    sent_to = await asyncio.to_thread(
                        send_wol, self._wol_macs(), self.host, self.subnet
                    )
                    print(
                        "[tv] Wake-on-LAN enviado"
                        + (
                            f" a {', '.join(sent_to)}"
                            if sent_to
                            else " (sin MAC válida)"
                        )
                    )
                    if not await self._wait_connected(WAKE_TIMEOUT):
                        self.state["error"] = (
                            "no se pudo encender la TV; "
                            "comprueba que no esté desenchufada"
                        )
                        raise CannotConnect("no se pudo encender la TV")
                    await self._wait_on(BOOT_TIMEOUT)
                finally:
                    self.state["waking"] = False
                action = "woke"

            if action != "launched":
                # Deja que el lanzador termine de arrancar antes de abrir la app.
                await asyncio.sleep(0.8)

            await self._run(lambda remote: remote.send_launch_app_command(app))
            print(f"[tv] app abierta: {app} ({action})")
            return {"action": action, "app": app}


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
    return web.json_response({"ok": True, **controller.status_payload()})


async def api_power(request: web.Request) -> web.Response:
    controller: TvController = request.app["controller"]
    try:
        result = await controller.power_toggle()
    except PowerBusy as exc:
        return json_error(str(exc), 409)
    except InvalidAuth:
        return json_error("No emparejado. Ejecuta ./tv pair en el PC.", 409)
    except (CannotConnect, ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
        return json_error(f"No se pudo cambiar el estado de la TV: {exc}", 502)
    return web.json_response({"ok": True, **result})


async def api_key(request: web.Request) -> web.Response:
    controller: TvController = request.app["controller"]
    if controller.state["waking"]:
        return json_error("La TV está despertando; espera unos segundos", 409)
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
        result = await controller.launch_app_waking(app)
    except PowerBusy as exc:
        return json_error(str(exc), 409)
    except InvalidAuth:
        return json_error("No emparejado. Ejecuta ./tv pair en el PC.", 409)
    except (CannotConnect, ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
        return json_error(f"No se pudo abrir la app: {exc}", 502)
    return web.json_response({"ok": True, **result})


async def api_sleep(request: web.Request) -> web.Response:
    controller: TvController = request.app["controller"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return json_error("JSON inválido")

    raw = data.get("minutes")
    if raw is None:
        minutes = None
    else:
        try:
            minutes = float(raw)
        except (TypeError, ValueError):
            return json_error("Minutos inválidos")
        if not math.isfinite(minutes) or minutes < 0 or minutes > MAX_SLEEP_MINUTES:
            return json_error(f"Minutos fuera de rango (0–{MAX_SLEEP_MINUTES})")

    result = await controller.set_sleep(minutes)
    return web.json_response({"ok": True, **result})


async def api_events(request: web.Request) -> web.StreamResponse:
    """Estado en vivo por Server-Sent Events (con latido cada 15 s)."""
    controller: TvController = request.app["controller"]
    controller.check_resume()

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)

    queue = controller.subscribe()
    try:
        initial = json.dumps(controller.status_payload())
        await response.write(f"data: {initial}\n\n".encode("utf-8"))
        while True:
            try:
                payload = await asyncio.wait_for(
                    queue.get(), timeout=EVENT_HEARTBEAT
                )
            except asyncio.TimeoutError:
                await response.write(b": latido\n\n")
                continue
            await response.write(f"data: {payload}\n\n".encode("utf-8"))
    except (asyncio.CancelledError, ConnectionResetError, ConnectionClosed):
        pass
    except OSError:
        pass
    finally:
        controller.unsubscribe(queue)
    return response


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
    if controller._sleep_task and not controller._sleep_task.done():
        controller._sleep_task.cancel()
    controller._drop()


def build_app(controller: TvController) -> web.Application:
    app = web.Application()
    app["controller"] = controller
    app.router.add_get("/", index)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/icons/{name}", icon)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/events", api_events)
    app.router.add_post("/api/power", api_power)
    app.router.add_post("/api/sleep", api_sleep)
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
