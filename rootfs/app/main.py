#!/usr/bin/env python3
"""
Flashforge Adventurer 5M Home Assistant Addon
Backend application for printer control and monitoring
Supports up to 100 printers
"""

import os
import json
import asyncio
import logging
import aiohttp
from aiohttp import web
from typing import Dict, Any, Optional, Callable, List
from enum import Enum

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

MAX_PRINTERS = 100

LOG_LEVELS = {'error': logging.ERROR, 'warning': logging.WARNING, 'info': logging.INFO, 'debug': logging.DEBUG}
log_level = os.environ.get('LOG_LEVEL', 'info').lower()
logging.basicConfig(level=LOG_LEVELS.get(log_level, logging.INFO), format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class PrinterState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"


class MoonrakerClient:
    def __init__(self, printer_id: str, printer_ip: str, port: int = 7125, api_key: Optional[str] = None):
        self.printer_id = printer_id
        self.printer_ip = printer_ip
        self.port = port
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._connected = False
        self._running = False
        self._message_callbacks: list = []
        self._printer_state = PrinterState.DISCONNECTED
        self._printer_data = {"extruder_temp": 0, "extruder_target": 0, "bed_temp": 0, "bed_target": 0, "progress": 0, "filename": "", "state": "disconnected", "print_duration": 0}
        
    @property
    def base_url(self) -> str:
        return f"http://{self.printer_ip}:{self.port}"
    
    @property
    def ws_url(self) -> str:
        return f"ws://{self.printer_ip}:{self.port}/websocket"
    
    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed
    
    @property
    def printer_data(self) -> Dict:
        return self._printer_data
    
    def add_message_callback(self, callback: Callable):
        self._message_callbacks.append(callback)
    
    def _set_state(self, state: PrinterState):
        self._printer_state = state
        self._printer_data["state"] = state.value
    
    async def connect(self) -> bool:
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(self.ws_url, heartbeat=30)
            self._connected = True
            self._running = True
            self._set_state(PrinterState.READY)
            logger.info(f"Connected to Moonraker at {self.printer_ip}:{self.port} (ID: {self.printer_id})")
            return True
        except Exception as e:
            logger.error(f"Connection failed to {self.printer_ip}:{self.port}: {e}")
            self._connected = False
            self._set_state(PrinterState.DISCONNECTED)
            return False
    
    async def disconnect(self):
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._set_state(PrinterState.DISCONNECTED)
    
    async def send_request(self, method: str, params: Optional[Dict] = None) -> Optional[Dict]:
        if not self.is_connected and not await self.connect():
            return None
        request_id = int(asyncio.get_event_loop().time() % 1000000)
        request = {"jsonrpc": "2.0", "method": method, "id": request_id}
        if params:
            request["params"] = params
        try:
            await self._ws.send_json(request)
            async with asyncio.timeout(30):
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("id") == request_id:
                            return data.get("result")
        except asyncio.TimeoutError:
            logger.error("Request timeout")
        except Exception as e:
            logger.error(f"Request error: {e}")
        return None
    
    async def get_printer_info(self) -> Optional[Dict]:
        try:
            if not self._session:
                self._session = aiohttp.ClientSession()
            async with self._session.get(f"{self.base_url}/printer/info", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("result", {})
        except Exception as e:
            logger.error(f"Get info error: {e}")
        return None
    
    async def send_gcode(self, gcode: str) -> bool:
        try:
            if not self._session:
                self._session = aiohttp.ClientSession()
            async with self._session.post(f"{self.base_url}/printer/gcode/script", json={"script": gcode}) as response:
                return response.status == 200
        except Exception as e:
            logger.error(f"G-code error: {e}")
            return False
    
    async def pause_print(self) -> bool:
        return await self.send_gcode("M25")
    
    async def resume_print(self) -> bool:
        return await self.send_gcode("M24")
    
    async def cancel_print(self) -> bool:
        return await self.send_gcode("M0")
    
    async def run_websocket_listener(self):
        while self._running:
            try:
                if not self.is_connected and not await self.connect():
                    await asyncio.sleep(5)
                    continue
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            await self._handle_message(data)
                        except json.JSONDecodeError:
                            pass
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        self._connected = False
                        break
                if self._running and not self.is_connected:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Listener error: {e}")
                if self._running:
                    await asyncio.sleep(5)
    
    async def _handle_message(self, data: Dict):
        if "params" in data and "objects" in data["params"]:
            objects = data["params"]["objects"]
            for callback in self._message_callbacks:
                try:
                    await callback(objects)
                except Exception as e:
                    logger.error(f"Callback error: {e}")
            if "extruder" in objects:
                self._printer_data["extruder_temp"] = objects["extruder"].get("temperature", 0)
                self._printer_data["extruder_target"] = objects["extruder"].get("target", 0)
            if "heater_bed" in objects:
                self._printer_data["bed_temp"] = objects["heater_bed"].get("temperature", 0)
                self._printer_data["bed_target"] = objects["heater_bed"].get("target", 0)
            if "display_status" in objects:
                self._printer_data["progress"] = objects["display_status"].get("progress", 0) * 100
            if "print_stats" in objects:
                stats = objects["print_stats"]
                self._printer_data["filename"] = stats.get("filename", "")
                self._printer_data["print_duration"] = stats.get("print_duration", 0)
                state = stats.get("state", "")
                if state == "printing":
                    self._set_state(PrinterState.PRINTING)
                elif state == "paused":
                    self._set_state(PrinterState.PAUSED)
                elif state == "complete":
                    self._set_state(PrinterState.COMPLETE)
                elif state == "cancelled":
                    self._set_state(PrinterState.CANCELLED)


class DiscoveryService:
    def __init__(self):
        self.found_devices: Dict[str, Dict] = {}
        self._zeroconf: Optional[Zeroconf] = None
    
    async def mdns_discovery(self, timeout: int = 5) -> list:
        if not ZEROCONF_AVAILABLE:
            return []
        services = {}
        def on_service_state_change(zeroconf, service_type, name, state_change):
            if state_change == ServiceStateChange.Added:
                info = zeroconf.get_service_info(service_type, name)
                if info:
                    addresses = [str(addr) for addr in info.addresses]
                    services[name] = {"name": name, "ip": addresses[0] if addresses else None, "port": info.port, "discovery_method": "mdns"}
        try:
            self._zeroconf = Zeroconf()
            browser = ServiceBrowser(self._zeroconf, "_moonraker._tcp.local.", [on_service_state_change])
            await asyncio.sleep(timeout)
            browser.cancel()
            if self._zeroconf:
                self._zeroconf.close()
        except Exception as e:
            logger.error(f"mDNS discovery error: {e}")
        return list(services.values())
    
    async def http_validate(self, ip: str, port: int = 7125) -> Optional[Dict]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{ip}:{port}/printer/info", timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {"valid": True, "ip": ip, "port": port, "type": "moonraker", "info": data.get("result", {})}
        except Exception as e:
            logger.debug(f"Validation failed for {ip}:{port} - {e}")
        return None
    
    async def scan_subnet(self, subnet: str, port: int = 7125, max_hosts: int = MAX_PRINTERS) -> list:
        from ipaddress import IPv4Network
        try:
            network = IPv4Network(subnet, strict=False)
            hosts = [str(host) for host in network.hosts()][:max_hosts]
        except Exception as e:
            logger.error(f"Invalid subnet: {e}")
            return []
        semaphore = asyncio.Semaphore(50)
        async def check_host(ip: str):
            async with semaphore:
                return await self.http_validate(ip, port)
        tasks = [check_host(ip) for ip in hosts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        found = [r for r in results if r and r.get("valid")]
        logger.info(f"Discovery complete: found {len(found)} printers in {subnet}")
        return found


class FlashforgeAddon:
    def __init__(self):
        self.printers: Dict[str, MoonrakerClient] = {}
        self.discovery = DiscoveryService()
        self.app = web.Application()
        self.setup_routes()
    
    def setup_routes(self):
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/health', self.handle_health)
        self.app.router.add_get('/api/printers', self.handle_printers_list)
        self.app.router.add_get('/api/printers/data', self.handle_all_printers_data)
        self.app.router.add_get('/api/discovery', self.handle_discovery)
        self.app.router.add_post('/api/printers/add', self.handle_add_printer)
        self.app.router.add_post('/api/printers/remove', self.handle_remove_printer)
        self.app.router.add_post('/api/printers/<printer_id>/pause', self.handle_pause)
        self.app.router.add_post('/api/printers/<printer_id>/resume', self.handle_resume)
        self.app.router.add_post('/api/printers/<printer_id>/cancel', self.handle_cancel)
        self.app.router.add_static('/static/', path='./static/', name='static')
    
    async def handle_index(self, request):
        return web.FileResponse('./static/index.html')
    
    async def handle_health(self, request):
        return web.Response(text='OK')
    
    async def handle_printers_list(self, request):
        printers_info = []
        for pid, printer in self.printers.items():
            info = {"id": pid, "ip": printer.printer_ip, "port": printer.port, "state": printer.printer_data.get("state", "disconnected"), "data": printer.printer_data}
            printers_info.append(info)
        return web.json_response({"printers": printers_info, "count": len(printers_info), "max_printers": MAX_PRINTERS})
    
    async def handle_all_printers_data(self, request):
        all_data = {}
        for pid, printer in self.printers.items():
            all_data[pid] = printer.printer_data
        return web.json_response(all_data)
    
    async def handle_discovery(self, request):
        subnet = request.query.get('subnet', '192.168.1.0/24')
        try:
            devices = await self.discovery.scan_subnet(subnet, 7125, MAX_PRINTERS)
        except Exception as e:
            devices = []
            logger.error(f"Discovery error: {e}")
        return web.json_response({'devices': devices, 'count': len(devices)})
    
    async def handle_add_printer(self, request):
        if len(self.printers) >= MAX_PRINTERS:
            return web.json_response({'error': f'Maximum {MAX_PRINTERS} printers reached'}, status=400)
        try:
            data = await request.json()
            printer_ip = data.get('printer_ip', '')
            printer_port = data.get('moonraker_port', 7125)
            printer_id = data.get('printer_id', f"printer_{len(self.printers) + 1}")
            if not printer_ip:
                return web.json_response({'error': 'printer_ip required'}, status=400)
            if printer_id in self.printers:
                return web.json_response({'error': 'Printer ID already exists'}, status=400)
            new_printer = MoonrakerClient(printer_id, printer_ip, printer_port)
            self.printers[printer_id] = new_printer
            asyncio.create_task(self._connect_printer(printer_id))
            return web.json_response({'status': 'ok', 'printer_id': printer_id, 'printer_ip': printer_ip})
        except Exception as e:
            logger.error(f"Add printer error: {e}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_remove_printer(self, request):
        try:
            data = await request.json()
            printer_id = data.get('printer_id', '')
            if printer_id in self.printers:
                await self.printers[printer_id].disconnect()
                del self.printers[printer_id]
                return web.json_response({'status': 'ok'})
            return web.json_response({'error': 'Printer not found'}, status=404)
        except Exception as e:
            logger.error(f"Remove printer error: {e}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_pause(self, request):
        printer_id = request.match_info.get('printer_id')
        if printer_id and printer_id in self.printers:
            if await self.printers[printer_id].pause_print():
                return web.json_response({'status': 'ok'})
        return web.json_response({'error': 'Failed to pause'}, status=500)
    
    async def handle_resume(self, request):
        printer_id = request.match_info.get('printer_id')
        if printer_id and printer_id in self.printers:
            if await self.printers[printer_id].resume_print():
                return web.json_response({'status': 'ok'})
        return web.json_response({'error': 'Failed to resume'}, status=500)
    
    async def handle_cancel(self, request):
        printer_id = request.match_info.get('printer_id')
        if printer_id and printer_id in self.printers:
            if await self.printers[printer_id].cancel_print():
                return web.json_response({'status': 'ok'})
        return web.json_response({'error': 'Failed to cancel'}, status=500)
    
    async def _connect_printer(self, printer_id: str):
        if printer_id in self.printers:
            printer = self.printers[printer_id]
            await printer.connect()
            await printer.subscribe({"heater_bed": ["temperature", "target"], "extruder": ["temperature", "target"], "print_stats": ["filename", "state"], "display_status": ["progress"]}, self._on_printer_data_update)
            asyncio.create_task(printer.run_websocket_listener())
    
    def _on_printer_data_update(self, objects: Dict):
        pass  # Callback for future use
    
    async def run(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8099)
        await site.start()
        logger.info(f"Application started on port 8099 (max printers: {MAX_PRINTERS})")
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            for printer in self.printers.values():
                await printer.disconnect()
            await runner.cleanup()


async def main():
    addon = FlashforgeAddon()
    await addon.run()


if __name__ == '__main__':
    asyncio.run(main())
