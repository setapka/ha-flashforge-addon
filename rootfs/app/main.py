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
log_level = os.environ.get('LOG_LEVEL', 'error').lower()
logging.basicConfig(level=LOG_LEVELS.get(log_level, logging.ERROR), format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
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
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            # Test connection by getting printer info
            async with self._session.get(f"{self.base_url}/getPrinterInfo", timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    self._connected = True
                    self._set_state(PrinterState.READY)
                    logger.info(f"Connected to Flashforge at {self.printer_ip}:{self.port}")
                    return True
            self._connected = False
            self._set_state(PrinterState.DISCONNECTED)
            return False
        except Exception as e:
            logger.debug(f"Connection failed to {self.printer_ip}:{self.port}: {e}")
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
                self._session = aiohttp.ClientSession()
            async with self._session.get(f"{self.base_url}/getPrinterStatus", timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._parse_status(data)
        except Exception as e:
            logger.debug(f"Get status error: {e}")
        return None
    
    def _parse_status(self, data: Dict) -> Dict:
        """Parse Flashforge status response"""
        # Flashforge API returns different structure than Moonraker
        result = data.get('result', {})
        
        # Temperature data
        if 'extruder' in result:
            self._printer_data["extruder_temp"] = float(result.get('extruder', {}).get('temperature', 0))
            self._printer_data["extruder_target"] = float(result.get('extruder', {}).get('target', 0))
        if 'bed' in result or 'heater_bed' in result:
            bed_data = result.get('bed', result.get('heater_bed', {}))
            self._printer_data["bed_temp"] = float(bed_data.get('temperature', 0))
            self._printer_data["bed_target"] = float(bed_data.get('target', 0))
        
        # Print status
        print_status = result.get('print_status', result.get('printState', ''))
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
        
        # Progress and file info
        self._printer_data["progress"] = float(result.get('progress', 0))
        self._printer_data["filename"] = result.get('filename', result.get('printFile', ''))
        self._printer_data["print_duration"] = int(result.get('printTime', result.get('print_duration', 0)))
        
        return self._printer_data
    
    async def send_gcode(self, gcode: str) -> bool:
        """Send G-code command to printer"""
        try:
            if not self._session:
                self._session = aiohttp.ClientSession()
            # Flashforge uses different endpoint for G-code
            async with self._session.post(f"{self.base_url}/sendGcode", 
                                          json={"gcode": gcode},
                                          timeout=aiohttp.ClientTimeout(total=10)) as response:
                return response.status == 200
        except Exception as e:
            logger.debug(f"G-code error: {e}")
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
    
    async def scan_subnet(self, subnet: str, port: int = 8899, max_hosts: int = MAX_PRINTERS) -> list:
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
                return await self._check_flashforge(ip, port)
        
        tasks = [check_host(ip) for ip in hosts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        found = [r for r in results if r and r.get("valid")]
        logger.info(f"Discovery complete: found {len(found)} printers in {subnet}")
        return found
    
    async def _check_flashforge(self, ip: str, port: int = 8899) -> Optional[Dict]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{ip}:{port}/getPrinterInfo", 
                                       timeout=aiohttp.ClientTimeout(total=3)) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {"valid": True, "ip": ip, "port": port, "type": "flashforge", "info": data}
        except Exception:
            pass
        return None


class FlashforgeAddon:
    def __init__(self):
        self.printers: Dict[str, FlashforgeClient] = {}
        self.discovery = DiscoveryService()
        self.app = web.Application()
        self.setup_routes()
        self._poll_task: Optional[asyncio.Task] = None
    
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
            info = {
                "id": pid,
                "ip": printer.printer_ip,
                "port": printer.port,
                "state": printer.printer_data.get("state", "disconnected"),
                "data": printer.printer_data
            }
            printers_info.append(info)
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
        subnet = request.query.get('subnet', '192.168.1.0/24')
        ports_str = request.query.get('ports', '8899')
        try:
            ports = [int(p.strip()) for p in ports_str.split(',') if p.strip()]
        except:
            ports = [8899]
        all_devices = []
        for port in ports:
            try:
                devices = await self.discovery.scan_subnet(subnet, port, MAX_PRINTERS // len(ports))
                all_devices.extend(devices)
            except Exception as e:
                logger.error(f"Discovery error on port {port}: {e}")
        return web.json_response({'devices': all_devices, 'count': len(all_devices), 'ports': ports})
    
    async def handle_add_printer(self, request):
        if len(self.printers) >= MAX_PRINTERS:
            return web.json_response({'error': f'Maximum {MAX_PRINTERS} printers reached'}, status=400)
        try:
            data = await request.json()
            printer_ip = data.get('printer_ip', '')
            # Support both old (moonraker_port) and new (printer_port) parameter names
            printer_port = data.get('printer_port', data.get('moonraker_port', 8899))
            printer_id = data.get('printer_id', f"printer_{len(self.printers) + 1}")
            
            if not printer_ip:
                return web.json_response({'error': 'printer_ip required'}, status=400)
            if printer_id in self.printers:
                return web.json_response({'error': 'Printer ID already exists'}, status=400)
            
            new_printer = FlashforgeClient(printer_id, printer_ip, printer_port)
            self.printers[printer_id] = new_printer
            
            # Try to connect
            connected = await new_printer.connect()
            if connected:
                await new_printer.get_status()
            
            return web.json_response({
                'status': 'ok' if connected else 'connected_false',
                'printer_id': printer_id,
                'printer_ip': printer_ip,
                'connected': connected
            })
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
    
    async def _poll_printers(self):
        """Periodically poll all printers for status updates"""
        while True:
            for printer in self.printers.values():
                if printer.is_connected:
                    try:
                        await printer.get_status()
                    except Exception as e:
                        logger.debug(f"Poll error for {printer.printer_id}: {e}")
            await asyncio.sleep(2)
    
    async def run(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8099)
        await site.start()
        logger.info(f"Application started on port 8099 (max printers: {MAX_PRINTERS})")
        
        # Start polling task
        self._poll_task = asyncio.create_task(self._poll_printers())
        
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            if self._poll_task:
                self._poll_task.cancel()
            for printer in self.printers.values():
                await printer.disconnect()
            await runner.cleanup()


async def main():
    addon = FlashforgeAddon()
    await addon.run()


if __name__ == '__main__':
    asyncio.run(main())
