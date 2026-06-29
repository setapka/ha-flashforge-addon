#!/usr/bin/env python3
"""
Flashforge Adventurer 5M Home Assistant Addon
Backend application for printer control and monitoring
"""

import os
import json
import asyncio
import logging
import aiohttp
from aiohttp import web
from typing import Dict, Any, Optional, Callable
from enum import Enum

# MQTT imports
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

# Zeroconf imports
try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

# Настройка логирования
LOG_LEVELS = {
    'error': logging.ERROR,
    'warning': logging.WARNING,
    'info': logging.INFO,
    'debug': logging.DEBUG
}

log_level = os.environ.get('LOG_LEVEL', 'info').lower()
logging.basicConfig(
    level=LOG_LEVELS.get(log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PrinterState(Enum):
    """Состояния принтера"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"


class MoonrakerClient:
    """Клиент для подключения к Moonraker API"""
    
    def __init__(self, printer_ip: str, port: int = 7125, api_key: Optional[str] = None):
        self.printer_ip = printer_ip
        self.port = port
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._connected = False
        self._subscriptions: Dict[str, Callable] = {}
        self._reconnect_delay = 5.0
        self._running = False
        self._state_callbacks: list = []
        self._message_callbacks: list = []
        self._printer_state = PrinterState.DISCONNECTED
        self._printer_data = {
            "extruder_temp": 0, "extruder_target": 0,
            "bed_temp": 0, "bed_target": 0,
            "progress": 0, "filename": "", "state": "disconnected"
        }
        
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
    
    def add_state_callback(self, callback: Callable):
        self._state_callbacks.append(callback)
    
    def add_message_callback(self, callback: Callable):
        self._message_callbacks.append(callback)
    
    def _set_state(self, state: PrinterState):
        old_state = self._printer_state
        self._printer_state = state
        self._printer_data["state"] = state.value
        for callback in self._state_callbacks:
            try:
                asyncio.create_task(callback(old_state, state))
            except:
                pass
    
    async def connect(self) -> bool:
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            headers = {"X-Api-Key": self.api_key} if self.api_key else {}
            self._ws = await self._session.ws_connect(
                self.ws_url, headers=headers, heartbeat=30
            )
            self._connected = True
            self._running = True
            self._set_state(PrinterState.READY)
            logger.info(f"Connected to Moonraker at {self.printer_ip}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
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
    
    async def subscribe(self, objects: Dict[str, list], callback: Callable):
        for obj_name in objects.keys():
            self._subscriptions[obj_name] = callback
        return await self.send_request("printer.objects.subscribe", {"objects": objects})
    
    async def send_gcode(self, gcode: str) -> bool:
        try:
            if not self._session:
                self._session = aiohttp.ClientSession()
            async with self._session.post(
                f"{self.base_url}/printer/gcode/script", json={"script": gcode}
            ) as response:
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
                    await asyncio.sleep(self._reconnect_delay)
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
                    await asyncio.sleep(self._reconnect_delay)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Listener error: {e}")
                if self._running:
                    await asyncio.sleep(self._reconnect_delay)
    
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
                state = stats.get("state", "")
                if state == "printing":
                    self._set_state(PrinterState.PRINTING)
                elif state == "paused":
                    self._set_state(PrinterState.PAUSED)
                elif state == "complete":
                    self._set_state(PrinterState.COMPLETE)


class DiscoveryService:
    """Сервис обнаружения принтеров в сети"""
    
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
                    services[name] = {
                        "name": name, "ip": addresses[0] if addresses else None,
                        "port": info.port, "discovery_method": "mdns"
                    }
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
                async with session.get(
                    f"http://{ip}:{port}/printer/info",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {"valid": True, "ip": ip, "port": port, "type": "moonraker", "info": data.get("result", {})}
        except Exception as e:
            logger.debug(f"Validation failed for {ip}:{port} - {e}")
        return None
    
    async def scan_subnet(self, subnet: str, port: int = 7125) -> list:
        from ipaddress import IPv4Network
        try:
            network = IPv4Network(subnet, strict=False)
            hosts = [str(host) for host in network.hosts()]
        except Exception as e:
            logger.error(f"Invalid subnet: {e}")
            return []
        semaphore = asyncio.Semaphore(50)
        async def check_host(ip: str):
            async with semaphore:
                result = await self.http_validate(ip, port)
                return result
        tasks = [check_host(ip) for ip in hosts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        found = [r for r in results if r and r.get("valid")]
        return found


class MQTTService:
    """Сервис для MQTT интеграции с Home Assistant"""
    
    def __init__(self, broker_host: str = "homeassistant", broker_port: int = 1883, device_id: str = "flashforge_adv5m"):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.device_id = device_id
        self.device_name = "Flashforge Adventurer 5M"
        self.client: Optional[mqtt.Client] = None
        self._connected = False
        if MQTT_AVAILABLE:
            # Используем MQTTv5 для совместимости
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.V2, client_id=f"{device_id}_addon")
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
    
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT broker")
            self._connected = True
            self.publish_discovery_configs()
        else:
            logger.error(f"MQTT connection failed with code {rc}")
            self._connected = False
    
    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
    
    def connect(self) -> bool:
        if not MQTT_AVAILABLE:
            return False
        try:
            self.client.connect(self.broker_host, self.broker_port, 60)
            self.client.loop_start()
            return True
        except Exception as e:
            logger.error(f"MQTT connection error: {e}")
            return False
    
    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
    
    def publish(self, topic: str, payload: Any, retain: bool = False):
        if not self._connected or not self.client:
            return
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, qos=1, retain=retain)
    
    def publish_discovery_configs(self):
        device_info = {"identifiers": [self.device_id], "name": self.device_name, "manufacturer": "Flashforge", "model": "Adventurer 5M"}
        self.publish("homeassistant/sensor/flashforge/extruder_temp/config", {"name": "Extruder Temperature", "device_class": "temperature", "unit_of_measurement": "°C", "state_topic": "flashforge/extruder/temperature", "unique_id": "flashforge_extruder_temp", "device": device_info}, retain=True)
        self.publish("homeassistant/sensor/flashforge/bed_temp/config", {"name": "Bed Temperature", "device_class": "temperature", "unit_of_measurement": "°C", "state_topic": "flashforge/bed/temperature", "unique_id": "flashforge_bed_temp", "device": device_info}, retain=True)
        self.publish("homeassistant/sensor/flashforge/print_progress/config", {"name": "Print Progress", "icon": "mdi:progress-clock", "state_topic": "flashforge/print/progress", "unit_of_measurement": "%", "unique_id": "flashforge_print_progress", "device": device_info}, retain=True)
        self.publish("homeassistant/sensor/flashforge/print_status/config", {"name": "Print Status", "icon": "mdi:printer-3d", "state_topic": "flashforge/print/status", "unique_id": "flashforge_print_status", "device": device_info}, retain=True)
    
    def update_state(self, state_type: str, data: Dict):
        topic_map = {
            "extruder": "flashforge/extruder/temperature",
            "bed": "flashforge/bed/temperature",
            "progress": "flashforge/print/progress",
            "status": "flashforge/print/status"
        }
        if state_type in topic_map:
            self.publish(topic_map[state_type], data)


class FlashforgeAddon:
    """Основное приложение аддона"""
    
    def __init__(self):
        self.printer_ip = os.environ.get('PRINTER_IP', '')
        self.moonraker_port = int(os.environ.get('MOONRAKER_PORT', '7125'))
        self.log_level = os.environ.get('LOG_LEVEL', 'info')
        self.moonraker = MoonrakerClient(self.printer_ip, self.moonraker_port) if self.printer_ip else None
        self.mqtt = MQTTService()
        self.discovery = DiscoveryService()
        self.app = web.Application()
        self.setup_routes()
    
    def setup_routes(self):
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/health', self.handle_health)
        self.app.router.add_get('/api/printer/info', self.handle_printer_info)
        self.app.router.add_get('/api/printer/data', self.handle_printer_data)
        self.app.router.add_get('/api/discovery', self.handle_discovery)
        self.app.router.add_post('/api/print/pause', self.handle_pause)
        self.app.router.add_post('/api/print/resume', self.handle_resume)
        self.app.router.add_post('/api/print/cancel', self.handle_cancel)
        self.app.router.add_post('/api/configure', self.handle_configure)
        self.app.router.add_static('/static/', path='./static/', name='static')
    
    async def handle_index(self, request):
        return web.FileResponse('./static/index.html')
    
    async def handle_health(self, request):
        return web.Response(text='OK')
    
    async def handle_printer_info(self, request):
        if not self.moonraker:
            return web.json_response({'error': 'Printer not configured'}, status=400)
        if not self.moonraker.is_connected and not await self.moonraker.connect():
            return web.json_response({'error': 'Cannot connect to printer'}, status=503)
        info = await self.moonraker.get_printer_info()
        return web.json_response({'result': info} if info else {'error': 'No response'})
    
    async def handle_printer_data(self, request):
        if self.moonraker:
            return web.json_response(self.moonraker.printer_data)
        return web.json_response({})
    
    async def handle_discovery(self, request):
        subnet = request.query.get('subnet', '192.168.1.0/24')
        try:
            if '/' in subnet:
                devices = await self.discovery.scan_subnet(subnet, self.moonraker_port)
            else:
                devices = await self.discovery.http_validate(subnet, self.moonraker_port)
                devices = [devices] if devices else []
        except Exception as e:
            devices = []
            logger.error(f"Discovery error: {e}")
        return web.json_response({'devices': devices})
    
    async def handle_pause(self, request):
        if self.moonraker and await self.moonraker.pause_print():
            return web.json_response({'status': 'ok'})
        return web.json_response({'error': 'Failed to pause'}, status=500)
    
    async def handle_resume(self, request):
        if self.moonraker and await self.moonraker.resume_print():
            return web.json_response({'status': 'ok'})
        return web.json_response({'error': 'Failed to resume'}, status=500)
    
    async def handle_cancel(self, request):
        if self.moonraker and await self.moonraker.cancel_print():
            return web.json_response({'status': 'ok'})
        return web.json_response({'error': 'Failed to cancel'}, status=500)
    
    async def handle_configure(self, request):
        try:
            data = await request.json()
            new_ip = data.get('printer_ip', '')
            new_port = data.get('moonraker_port', 7125)
            if new_ip:
                self.printer_ip = new_ip
                self.moonraker_port = new_port
                self.moonraker = MoonrakerClient(new_ip, new_port)
                return web.json_response({'status': 'ok', 'printer_ip': new_ip})
        except Exception as e:
            logger.error(f"Configure error: {e}")
        return web.json_response({'error': 'Invalid config'}, status=400)
    
    async def on_printer_data_update(self, objects: Dict):
        """Callback для обновления данных принтера"""
        if self.mqtt and self.mqtt._connected:
            if "extruder" in objects:
                self.mqtt.update_state("extruder", objects["extruder"])
            if "heater_bed" in objects:
                self.mqtt.update_state("bed", objects["heater_bed"])
            if "display_status" in objects:
                self.mqtt.update_state("progress", {"progress": objects["display_status"].get("progress", 0) * 100})
            if "print_stats" in objects:
                self.mqtt.update_state("status", {"status": objects["print_stats"].get("state", "unknown")})
    
    async def run(self):
        """Запуск приложения"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8099)
        await site.start()
        logger.info("Application started on port 8099")
        
        # Подключение MQTT
        self.mqtt.connect()
        
        # Подключение к принтеру и запуск WebSocket
        if self.moonraker and self.printer_ip:
            self.moonraker.add_message_callback(self.on_printer_data_update)
            await self.moonraker.connect()
            # Подписка на обновления
            await self.moonraker.subscribe({
                "heater_bed": ["temperature", "target"],
                "extruder": ["temperature", "target"],
                "print_stats": ["filename", "state", "progress"],
                "display_status": ["progress"]
            }, self.on_printer_data_update)
            # Запуск слушателя WebSocket
            ws_task = asyncio.create_task(self.moonraker.run_websocket_listener())
        
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            if self.moonraker:
                await self.moonraker.disconnect()
            self.mqtt.disconnect()
            await runner.cleanup()


async def main():
    addon = FlashforgeAddon()
    await addon.run()


if __name__ == '__main__':
    asyncio.run(main())
