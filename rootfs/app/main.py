#!/usr/bin/env python3
"""
Flashforge Adventurer 5M Home Assistant Addon
Backend application for printer control and monitoring
Supports up to 100 printers - Native Flashforge API (port 8899)
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
    from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

MQTT_AVAILABLE = False
MAX_PRINTERS = 100

LOG_LEVELS = {'error': logging.ERROR, 'warning': logging.WARNING, 'info': logging.INFO, 'debug': logging.DEBUG}
log_level = os.environ.get('LOG_LEVEL', 'info').lower()
logging.basicConfig(
    level=LOG_LEVELS.get(log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVELS.get(log_level, logging.INFO))


class PrinterState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"


class FlashforgeClient:
    """Client for Flashforge Adventurer 5M native API (port 8899)"""
    
    def __init__(self, printer_id: str, printer_ip: str, port: int = 8899):
        self.printer_id = printer_id
        self.printer_ip = printer_ip
        self.port = port
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        self._printer_state = PrinterState.DISCONNECTED
        self._printer_data = {
            "extruder_temp": 0, "extruder_target": 0,
            "bed_temp": 0, "bed_target": 0,
            "progress": 0, "filename": "",
            "state": "disconnected", "print_duration": 0
        }
        
    @property
    def base_url(self) -> str:
        return f"http://{self.printer_ip}:{self.port}"
    
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    @property
    def printer_data(self) -> Dict:
        return self._printer_data
    
    def _set_state(self, state: PrinterState):
        self._printer_state = state
        self._printer_data["state"] = state.value
    
    async def connect(self) -> bool:
        """Connect to Flashforge printer and get initial status"""
        logger.info(f"[{self.printer_id}] Connecting to {self.base_url}...")
        
        # Flashforge Adventurer 5M API endpoints
        endpoints = [
            "/getPrinterInfo",      # Moonraker/Klipper
            "/apis/printer/info",    # Alternative Moonraker
            "/api/printer",          # OctoPrint compatibility
            "/",                     # Root - check if web server responds
        ]
        
        try:
            if self._session is None or self._session.closed:
                logger.debug(f"[{self.printer_id}] Creating new HTTP session")
                self._session = aiohttp.ClientSession()
            
            for endpoint in endpoints:
                try:
                    url = f"{self.base_url}{endpoint}"
                    logger.debug(f"[{self.printer_id}] Trying {url}")
                    async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                        logger.debug(f"[{self.printer_id}] Response status: {response.status}")
                        if response.status == 200:
                            try:
                                data = await response.json()
                                logger.info(f"[{self.printer_id}] Response: {json.dumps(data)[:200]}")
                            except:
                                text = await response.text()
                                logger.info(f"[{self.printer_id}] Response (text): {text[:200]}")
                            
                            self._connected = True
                            self._set_state(PrinterState.READY)
                            logger.info(f"Connected to Flashforge at {self.printer_ip}:{self.port} (ID: {self.printer_id}) via {endpoint}")
                            return True
                        else:
                            logger.debug(f"[{self.printer_id}] Endpoint {endpoint} returned HTTP {response.status}")
                except Exception as e:
                    logger.debug(f"[{self.printer_id}] Endpoint {endpoint} failed: {type(e).__name__}: {e}")
                    continue
            
            logger.warning(f"[{self.printer_id}] All endpoints failed")
            self._connected = False
            self._set_state(PrinterState.DISCONNECTED)
            return False
            
        except asyncio.TimeoutError as e:
            logger.warning(f"[{self.printer_id}] Connection timeout to {self.printer_ip}:{self.port}")
            self._connected = False
            self._set_state(PrinterState.DISCONNECTED)
            return False
        except aiohttp.ClientError as e:
            logger.warning(f"[{self.printer_id}] Client error: {type(e).__name__}: {e}")
            self._connected = False
            self._set_state(PrinterState.DISCONNECTED)
            return False
        except Exception as e:
            logger.error(f"[{self.printer_id}] Unexpected connection error: {type(e).__name__}: {e}")
            self._connected = False
            self._set_state(PrinterState.DISCONNECTED)
            return False
    
    async def disconnect(self):
        self._connected = False
        self._set_state(PrinterState.DISCONNECTED)
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def get_status(self) -> Optional[Dict]:
        """Get printer status from Flashforge API"""
        try:
            if not self._session:
                logger.debug(f"[{self.printer_id}] Creating new session for status request")
                self._session = aiohttp.ClientSession()
            logger.debug(f"[{self.printer_id}] Getting status from {self.base_url}/getPrinterStatus")
            async with self._session.get(f"{self.base_url}/getPrinterStatus", timeout=aiohttp.ClientTimeout(total=5)) as response:
                logger.debug(f"[{self.printer_id}] Status response: {response.status}")
                if response.status == 200:
                    data = await response.json()
                    parsed = self._parse_status(data)
                    logger.debug(f"[{self.printer_id}] Status parsed: {parsed}")
                    return parsed
                else:
                    logger.warning(f"[{self.printer_id}] Status request failed - HTTP {response.status}")
        except asyncio.TimeoutError as e:
            logger.debug(f"[{self.printer_id}] Status timeout: {e}")
        except aiohttp.ClientError as e:
            logger.debug(f"[{self.printer_id}] Status client error: {type(e).__name__}: {e}")
        except Exception as e:
            logger.debug(f"[{self.printer_id}] Status unexpected error: {type(e).__name__}: {e}")
        return None
    
    def _parse_status(self, data: Dict) -> Dict:
        """Parse Flashforge status response"""
        logger.debug(f"[{self.printer_id}] Parsing status data: {json.dumps(data, indent=2)[:500]}")
        result = data.get('result', {})
        
        if 'extruder' in result:
            self._printer_data["extruder_temp"] = float(result.get('extruder', {}).get('temperature', 0))
            self._printer_data["extruder_target"] = float(result.get('extruder', {}).get('target', 0))
            logger.debug(f"[{self.printer_id}] Extruder: {self._printer_data['extruder_temp']}°C (target: {self._printer_data['extruder_target']}°C)")
        
        if 'bed' in result or 'heater_bed' in result:
            bed_data = result.get('bed', result.get('heater_bed', {}))
            self._printer_data["bed_temp"] = float(bed_data.get('temperature', 0))
            self._printer_data["bed_target"] = float(bed_data.get('target', 0))
            logger.debug(f"[{self.printer_id}] Bed: {self._printer_data['bed_temp']}°C (target: {self._printer_data['bed_target']}°C)")
        
        print_status = result.get('print_status', result.get('printState', ''))
        old_state = self._printer_data["state"]
        if print_status == 'printing':
            self._set_state(PrinterState.PRINTING)
        elif print_status == 'pause':
            self._set_state(PrinterState.PAUSED)
        elif print_status == 'completed':
            self._set_state(PrinterState.COMPLETE)
        elif print_status == 'cancel':
            self._set_state(PrinterState.CANCELLED)
        elif print_status:
            self._set_state(PrinterState.READY)
        
        if old_state != self._printer_data["state"]:
            logger.info(f"[{self.printer_id}] State changed: {old_state} -> {self._printer_data['state']}")
        
        self._printer_data["progress"] = float(result.get('progress', 0))
        self._printer_data["filename"] = result.get('filename', result.get('printFile', ''))
        self._printer_data["print_duration"] = int(result.get('printTime', result.get('print_duration', 0)))
        
        logger.debug(f"[{self.printer_id}] Progress: {self._printer_data['progress']}%, File: {self._printer_data['filename']}")
        return self._printer_data
    
    async def send_gcode(self, gcode: str) -> bool:
        """Send G-code command to printer"""
        logger.info(f"[{self.printer_id}] Sending G-code: {gcode}")
        try:
            if not self._session:
                self._session = aiohttp.ClientSession()
            async with self._session.post(f"{self.base_url}/sendGcode", 
                                          json={"gcode": gcode},
                                          timeout=aiohttp.ClientTimeout(total=10)) as response:
                logger.debug(f"[{self.printer_id}] G-code response: {response.status}")
                if response.status == 200:
                    logger.info(f"[{self.printer_id}] G-code '{gcode}' sent successfully")
                    return True
                else:
                    logger.warning(f"[{self.printer_id}] G-code failed - HTTP {response.status}")
                    return False
        except asyncio.TimeoutError as e:
            logger.warning(f"[{self.printer_id}] G-code timeout for '{gcode}'")
            return False
        except Exception as e:
            logger.error(f"[{self.printer_id}] G-code error: {type(e).__name__}: {e}")
            return False
    
    async def pause_print(self) -> bool:
        return await self.send_gcode("M25")
    
    async def resume_print(self) -> bool:
        return await self.send_gcode("M24")
    
    async def cancel_print(self) -> bool:
        return await self.send_gcode("M0")


class DiscoveryService:
    def __init__(self):
        self.found_devices: Dict[str, Dict] = {}
        self._zeroconf: Optional[Zeroconf] = None
        self._hosts_checked = 0
        self._printers_found = 0
    
    async def scan_subnet(self, subnet: str, port: int = 8899, max_hosts: int = MAX_PRINTERS) -> list:
        from ipaddress import IPv4Network
        logger.info(f"Starting discovery scan: subnet={subnet}, port={port}, max_hosts={max_hosts}")
        self._hosts_checked = 0
        self._printers_found = 0
        
        try:
            network = IPv4Network(subnet, strict=False)
            hosts = [str(host) for host in network.hosts()][:max_hosts]
            logger.info(f"Network parsed: {len(hosts)} hosts to check in {subnet}")
        except Exception as e:
            logger.error(f"Invalid subnet '{subnet}': {e}")
            return []
        
        semaphore = asyncio.Semaphore(50)
        
        async def check_host(ip: str):
            async with semaphore:
                try:
                    result = await self._check_flashforge(ip, port)
                    if result:
                        self._printers_found += 1
                    self._hosts_checked += 1
                    return result
                except Exception as e:
                    logger.debug(f"Check host {ip} error: {e}")
                    self._hosts_checked += 1
                    return None
        
        logger.info(f"Launching {len(hosts)} concurrent host checks...")
        tasks = [check_host(ip) for ip in hosts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        found = []
        for r in results:
            if r is not None and isinstance(r, dict) and r.get("valid"):
                found.append(r)
                logger.info(f"Printer found: {r['ip']}:{r['port']}")
        
        logger.info(f"Discovery complete: checked {self._hosts_checked} hosts, found {len(found)} printers in {subnet}")
        return found
    
    async def _check_flashforge(self, ip: str, port: int = 8899) -> Optional[Dict]:
        """Check if host is a Flashforge printer by trying multiple endpoints"""
        endpoints = ["/getPrinterInfo", "/apis/printer/info", "/api/printer", "/"]
        
        for endpoint in endpoints:
            try:
                url = f"http://{ip}:{port}{endpoint}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as response:
                        if response.status == 200:
                            try:
                                data = await response.json()
                                logger.debug(f"Host {ip}:{port}{endpoint} responded: {json.dumps(data)[:100]}")
                                return {"valid": True, "ip": ip, "port": port, "type": "flashforge", "info": data}
                            except:
                                logger.debug(f"Host {ip}:{port}{endpoint} returned HTML/text - likely web server")
                                return {"valid": True, "ip": ip, "port": port, "type": "unknown", "info": {}}
            except asyncio.TimeoutError:
                logger.debug(f"Host {ip}:{port}{endpoint} timeout")
                continue
            except aiohttp.ClientError:
                logger.debug(f"Host {ip}:{port}{endpoint} client error")
                continue
            except Exception as e:
                logger.debug(f"Host {ip}:{port}{endpoint} error: {type(e).__name__}")
                continue
        
        return None


class FlashforgeAddon:
    def __init__(self):
        self.printers: Dict[str, FlashforgeClient] = {}
        self.discovery = DiscoveryService()
        self.app = web.Application()
        self.setup_routes()
        self._poll_task: Optional[asyncio.Task] = None
        self._config_printers: List[Dict] = []
    
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
        logger.debug("Handling /api/printers request")
        printers_info = []
        for pid, printer in self.printers.items():
            info = {
                "id": pid,
                "ip": printer.printer_ip,
                "port": printer.port,
                "state": printer.printer_data.get("state", "disconnected"),
                "data": printer.printer_data
            }
            printers_info.append(info)
        logger.debug(f"Returning {len(printers_info)} printers")
        return web.json_response({
            "printers": printers_info,
            "count": len(printers_info),
            "max_printers": MAX_PRINTERS
        })
    
    async def handle_all_printers_data(self, request):
        all_data = {}
        for pid, printer in self.printers.items():
            all_data[pid] = printer.printer_data
        return web.json_response(all_data)
    
    async def handle_discovery(self, request):
        # Get subnet from query parameter or environment variable
        default_subnet = os.environ.get('SCAN_SUBNET', '192.168.1.0/24')
        subnet = request.query.get('subnet', default_subnet)
        ports_str = request.query.get('ports', '8899')
        logger.info(f"Discovery API called: subnet={subnet}, ports={ports_str}")
        try:
            ports = [int(p.strip()) for p in ports_str.split(',') if p.strip()]
        except Exception as e:
            logger.warning(f"Failed to parse ports '{ports_str}': {e}")
            ports = [8899]
        all_devices = []
        for port in ports:
            try:
                logger.info(f"Scanning port {port}...")
                devices = await self.discovery.scan_subnet(subnet, port, MAX_PRINTERS // len(ports))
                all_devices.extend(devices)
            except Exception as e:
                logger.error(f"Discovery error on port {port}: {type(e).__name__}: {e}")
        logger.info(f"Discovery API returning {len(all_devices)} devices")
        return web.json_response({'devices': all_devices, 'count': len(all_devices), 'ports': ports, 'subnet': subnet})
    
    async def handle_add_printer(self, request):
        logger.info("Handling /api/printers/add request")
        if len(self.printers) >= MAX_PRINTERS:
            logger.warning(f"Add printer rejected: max printers ({MAX_PRINTERS}) reached")
            return web.json_response({'error': f'Maximum {MAX_PRINTERS} printers reached'}, status=400)
        try:
            data = await request.json()
            printer_ip = data.get('printer_ip', '')
            printer_port = data.get('printer_port', data.get('moonraker_port', 8899))
            printer_id = data.get('printer_id', f"printer_{len(self.printers) + 1}")
            
            logger.info(f"Adding printer: id={printer_id}, ip={printer_ip}, port={printer_port}")
            
            if not printer_ip:
                logger.warning("Add printer rejected: printer_ip required")
                return web.json_response({'error': 'printer_ip required'}, status=400)
            if printer_id in self.printers:
                logger.warning(f"Add printer rejected: ID {printer_id} already exists")
                return web.json_response({'error': 'Printer ID already exists'}, status=400)
            
            new_printer = FlashforgeClient(printer_id, printer_ip, printer_port)
            self.printers[printer_id] = new_printer
            
            logger.info(f"Connecting to new printer {printer_id} at {printer_ip}:{printer_port}...")
            connected = await new_printer.connect()
            if connected:
                logger.info(f"Printer {printer_id} connected successfully, getting status...")
                await new_printer.get_status()
            else:
                logger.warning(f"Printer {printer_id} connection failed")
            
            return web.json_response({
                'status': 'ok' if connected else 'connected_false',
                'printer_id': printer_id,
                'printer_ip': printer_ip,
                'connected': connected
            })
        except Exception as e:
            logger.error(f"Add printer error: {type(e).__name__}: {e}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_remove_printer(self, request):
        try:
            data = await request.json()
            printer_id = data.get('printer_id', '')
            logger.info(f"Handling /api/printers/remove request for {printer_id}")
            
            if printer_id in self.printers:
                logger.info(f"Removing printer {printer_id}")
                await self.printers[printer_id].disconnect()
                del self.printers[printer_id]
                return web.json_response({'status': 'ok'})
            else:
                logger.warning(f"Remove printer failed: {printer_id} not found")
                return web.json_response({'error': 'Printer not found'}, status=404)
        except Exception as e:
            logger.error(f"Remove printer error: {type(e).__name__}: {e}")
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
    
    async def _poll_printers(self):
        """Periodically poll all printers for status updates"""
        poll_count = 0
        while True:
            poll_count += 1
            connected_count = sum(1 for p in self.printers.values() if p.is_connected)
            logger.debug(f"Poll cycle {poll_count}: {connected_count} connected printers")
            
            for printer in self.printers.values():
                if printer.is_connected:
                    try:
                        await printer.get_status()
                    except Exception as e:
                        logger.debug(f"Poll error for {printer.printer_id}: {type(e).__name__}: {e}")
            await asyncio.sleep(2)
    
    async def _auto_connect_config_printers(self):
        """Auto-connect to printers from config on startup"""
        printer_ip = os.environ.get('PRINTER_IP', '')
        printer_port = int(os.environ.get('PRINTER_PORT', os.environ.get('MOONRAKER_PORT', '8899')))
        
        if printer_ip:
            logger.info(f"Auto-connecting to printer from config: {printer_ip}:{printer_port}")
            printer_id = f"printer_{printer_ip.replace('.', '_')}"
            if printer_id not in self.printers:
                new_printer = FlashforgeClient(printer_id, printer_ip, printer_port)
                self.printers[printer_id] = new_printer
                connected = await new_printer.connect()
                if connected:
                    await new_printer.get_status()
                    logger.info(f"Auto-connected to {printer_ip}:{printer_port}")
                else:
                    logger.warning(f"Failed to auto-connect to {printer_ip}:{printer_port}")
    
    async def run(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8099)
        await site.start()
        logger.info("=" * 50)
        logger.info(f"Application started on port 8099 (max printers: {MAX_PRINTERS})")
        logger.info(f"Log level: {log_level.upper()}")
        logger.info(f"Scan subnet: {os.environ.get('SCAN_SUBNET', '192.168.1.0/24')}")
        logger.info(f"Scan ports: {os.environ.get('SCAN_PORTS', '8899')}")
        logger.info("=" * 50)
        
        # Auto-connect to config printers
        await self._auto_connect_config_printers()
        
        # Start polling task
        self._poll_task = asyncio.create_task(self._poll_printers())
        logger.info("Polling task started")
        
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Application shutdown requested")
        finally:
            logger.info("Cleaning up...")
            if self._poll_task:
                self._poll_task.cancel()
            for printer in self.printers.values():
                await printer.disconnect()
            await runner.cleanup()
            logger.info("Application stopped")


async def main():
    addon = FlashforgeAddon()
    await addon.run()


if __name__ == '__main__':
    asyncio.run(main())
