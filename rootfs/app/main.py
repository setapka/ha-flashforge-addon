#!/usr/bin/env python3
"""
Flashforge Adventurer 5M Home Assistant Addon
Backend application for printer control and monitoring

Supports:
- TCP Protocol (port 8899): G-code commands, no authentication required
- HTTP REST API (port 8898): JSON-based, requires CheckCode authentication
- UDP Discovery Protocol: Auto-discovery via multicast/broadcast
"""

import os
import json
import asyncio
import logging
import re
import aiohttp
from aiohttp import web
from typing import Dict, Any, Optional, List, Tuple
from enum import Enum
from dataclasses import dataclass, field

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


# Flashforge Adventurer 5M TCP Protocol Commands
STATUS_COMMAND = b"~M601 S1\r\n"
PRINT_JOB_INFO_COMMAND = b"~M27\r\n"
TEMPERATURE_COMMAND = b"~M105\r\n"
STATE_COMMAND = b"~M119\r\n"

# Regex patterns for parsing responses
TEMPERATURE_REPLY_REGEX = re.compile(
    rb"CMD M105 Received\.\r\nT0:(\d+\.\d)/(\d+\.\d) T1:(\d+\.\d)/(\d+\.\d) B:(\d+\.\d)/(\d+\.\d)\r\nok\r\n"
)
STATE_REGEX = re.compile(
    rb"CMD M119 Received\..+\r\nMachineStatus: (\w+)\r\nMoveMode: ([^\r\n]+)\r\nStatus: S:(\d) L:(\d) J:(\d) F:(\d)\r\n[^\r\n]*\r\nCurrentFile:([^\r\n]+)\r\nok\r\n"
)
STATUS_REPLY_REGEX = re.compile(
    rb"CMD M27 Received\.\r\n\w+ printing byte (\d+)/(\d+)\r\nLayer: (\d+)/(\d+)\r\nnok\r\n"
)


@dataclass
class PrinterCredentials:
    """Credentials for HTTP REST API authentication"""
    serial_number: str = ""
    check_code: str = ""
    is_valid: bool = False


class HttpClient:
    """HTTP REST API Client for Flashforge printers (port 8898)"""
    
    def __init__(self, ip: str, port: int = 8898, credentials: Optional[PrinterCredentials] = None):
        self.ip = ip
        self.port = port
        self.base_url = f"http://{ip}:{port}"
        self.credentials = credentials or PrinterCredentials()
        self._session: Optional[aiohttp.ClientSession] = None
        self._auth_token: Optional[str] = None
        
    async def connect(self) -> bool:
        """Test HTTP connection and get auth token"""
        try:
            self._session = aiohttp.ClientSession()
            
            # Try to authenticate and get token
            if self.credentials.serial_number and self.credentials.check_code:
                auth_data = {
                    "serialNumber": self.credentials.serial_number,
                    "checkCode": self.credentials.check_code
                }
                async with self._session.post(
                    f"{self.base_url}/detail",
                    json=auth_data,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        self.credentials.is_valid = True
                        logger.info(f"HTTP API authenticated for {self.ip}")
                        return True
                    else:
                        logger.warning(f"HTTP API auth failed for {self.ip}: {resp.status}")
                        self.credentials.is_valid = False
                        return False
            return False
        except Exception as e:
            logger.debug(f"HTTP API connection error: {e}")
            if self._session:
                await self._session.close()
                self._session = None
            return False
    
    async def disconnect(self):
        """Close HTTP session"""
        if self._session:
            await self._session.close()
            self._session = None
    
    async def get_status(self) -> Optional[Dict]:
        """Get printer status via HTTP REST API"""
        if not self._session or not self.credentials.is_valid:
            return None
        
        try:
            async with self._session.get(
                f"{self.base_url}/status",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.debug(f"HTTP get_status error: {e}")
        return None
    
    async def get_temperatures(self) -> Optional[Dict]:
        """Get temperatures via HTTP REST API"""
        if not self._session or not self.credentials.is_valid:
            return None
        
        try:
            async with self._session.get(
                f"{self.base_url}/temperatures",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.debug(f"HTTP get_temperatures error: {e}")
        return None
    
    async def send_gcode(self, gcode: str) -> bool:
        """Send G-code via HTTP REST API"""
        if not self._session or not self.credentials.is_valid:
            return False
        
        try:
            auth_data = {
                "serialNumber": self.credentials.serial_number,
                "checkCode": self.credentials.check_code,
                "gcode": gcode
            }
            async with self._session.post(
                f"{self.base_url}/gcode",
                json=auth_data,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    logger.info(f"HTTP G-code '{gcode}' sent successfully")
                    return True
        except Exception as e:
            logger.debug(f"HTTP send_gcode error: {e}")
        return False
    
    async def upload_gcode(self, filename: str, gcode_data: bytes) -> bool:
        """Upload G-code file via HTTP REST API"""
        if not self._session or not self.credentials.is_valid:
            return False
        
        try:
            form = aiohttp.FormData()
            form.add_field('serialNumber', self.credentials.serial_number)
            form.add_field('checkCode', self.credentials.check_code)
            form.add_field('file', gcode_data, filename=filename)
            
            async with self._session.post(
                f"{self.base_url}/uploadGcode",
                data=form,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    logger.info(f"HTTP G-code file '{filename}' uploaded successfully")
                    return True
        except Exception as e:
            logger.error(f"HTTP upload_gcode error: {e}")
        return False


class FlashforgeClient:
    """
    Flashforge Adventurer 5M Client with dual protocol support:
    - TCP Protocol (port 8899): G-code commands, no authentication
    - HTTP REST API (port 8898): JSON-based, requires CheckCode auth
    """
    
    def __init__(self, printer_id: str, printer_ip: str, port: int = 8899, 
                 serial_number: str = "", check_code: str = ""):
        self.printer_id = printer_id
        self.printer_ip = printer_ip
        self.port = port
        self.http_port = 8898
        
        # TCP connection
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._printer_state = PrinterState.DISCONNECTED
        
        # HTTP client
        self.credentials = PrinterCredentials(
            serial_number=serial_number,
            check_code=check_code
        )
        self.http_client: Optional[HttpClient] = None
        
        # Printer data
        self._printer_data = {
            "extruder_temp": 0.0, "extruder_target": 0.0,
            "bed_temp": 0.0, "bed_target": 0.0,
            "progress": 0, "filename": "",
            "state": "disconnected", "print_duration": 0,
            "machine_status": "", "move_mode": "",
            "serial_number": "", "check_code": "",
            "http_authenticated": False
        }
        self._lock = asyncio.Lock()
        
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    @property
    def printer_data(self) -> Dict:
        return self._printer_data
    
    @property
    def serial_number(self) -> str:
        return self._printer_data.get("serial_number", "")
    
    @property
    def check_code(self) -> str:
        return self._printer_data.get("check_code", "")
    
    def _set_state(self, state: PrinterState):
        self._printer_state = state
        self._printer_data["state"] = state.value
    
    async def connect(self) -> bool:
        """Connect to Flashforge printer via TCP socket and HTTP REST API"""
        logger.info(f"[{self.printer_id}] Connecting to {self.printer_ip}:{self.port}...")
        
        tcp_connected = False
        http_connected = False
        
        # Connect via TCP (port 8899) - no auth required
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.printer_ip, self.port),
                timeout=5.0
            )
            tcp_connected = True
            logger.info(f"[{self.printer_id}] TCP connected to {self.printer_ip}:{self.port}")
            
            # Get credentials via TCP
            await self._get_credentials()
            
        except Exception as e:
            logger.warning(f"[{self.printer_id}] TCP connection error: {e}")
        
        # Connect via HTTP (port 8898) - requires auth
        if self.credentials.serial_number and self.credentials.check_code:
            self.http_client = HttpClient(
                self.printer_ip, 
                self.http_port, 
                self.credentials
            )
            http_connected = await self.http_client.connect()
            self._printer_data["http_authenticated"] = http_connected
            if http_connected:
                logger.info(f"[{self.printer_id}] HTTP REST API authenticated")
        
        self._connected = tcp_connected
        self._set_state(PrinterState.READY if tcp_connected else PrinterState.DISCONNECTED)
        
        if tcp_connected:
            await self.get_status()
        
        return tcp_connected
    
    async def _get_credentials(self):
        """Get serial number and check code from printer via TCP
        
        Command: M9000 - Get device info
        Response format varies by firmware version
        """
        # Try to get device info
        response = await self._send_command(b"~M9000\r\n")
        if response:
            try:
                text = response.decode('utf-8', errors='ignore')
                # Parse response for serial number
                # Format: "Device Info:\r\nSerial: SNXXXXX\r\n..."
                for line in text.split('\r\n'):
                    if 'Serial:' in line or 'SN:' in line:
                        sn_match = re.search(r'SN[:\s]*([A-Za-z0-9]+)', line)
                        if sn_match:
                            self._printer_data["serial_number"] = sn_match.group(1)
                            self.credentials.serial_number = sn_match.group(1)
                    if 'CheckCode:' in line or 'VerifyCode:' in line:
                        cc_match = re.search(r'(?:CheckCode|VerifyCode)[:\s]*(\d+)', line)
                        if cc_match:
                            self._printer_data["check_code"] = cc_match.group(1)
                            self.credentials.check_code = cc_match.group(1)
                logger.debug(f"[{self.printer_id}] Credentials: SN={self.credentials.serial_number}, CC={self.credentials.check_code}")
            except Exception as e:
                logger.debug(f"[{self.printer_id}] Parse credentials error: {e}")
        
        # Alternative: use discovery data if available
        if not self.credentials.serial_number:
            # Try M115 for firmware info
            response = await self._send_command(b"~M115\r\n")
            if response:
                text = response.decode('utf-8', errors='ignore')
                sn_match = re.search(r'SN[:\s]*([A-Za-z0-9]+)', text)
                if sn_match:
                    self._printer_data["serial_number"] = sn_match.group(1)
                    self.credentials.serial_number = sn_match.group(1)
    
    async def disconnect(self):
        """Disconnect from printer"""
        self._connected = False
        self._set_state(PrinterState.DISCONNECTED)
        
        # Disconnect HTTP client
        if self.http_client:
            await self.http_client.disconnect()
            self.http_client = None
        
        # Disconnect TCP client
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except:
                pass
            self._writer = None
            self._reader = None
    
    async def _send_command(self, command: bytes, timeout: float = 2.0) -> Optional[bytes]:
        """Send G-code command and read response"""
        async with self._lock:
            if not self._writer or not self._reader:
                logger.warning(f"[{self.printer_id}] Not connected")
                return None
            
            try:
                self._writer.write(command)
                await self._writer.drain()
                response = await asyncio.wait_for(self._reader.read(4096), timeout=timeout)
                logger.debug(f"[{self.printer_id}] Command {command.strip()} -> {response.strip()}")
                return response
            except asyncio.TimeoutError:
                logger.debug(f"[{self.printer_id}] Command timeout for {command.strip()}")
                return None
            except Exception as e:
                logger.error(f"[{self.printer_id}] Command error: {type(e).__name__}: {e}")
                return None
    
    async def get_status(self) -> Optional[Dict]:
        """Get printer status via TCP commands"""
        if not self._connected:
            return None
        
        # Get temperature
        temp_response = await self._send_command(TEMPERATURE_COMMAND)
        if temp_response:
            self._parse_temperature(temp_response)
        
        # Get state
        state_response = await self._send_command(STATE_COMMAND)
        if state_response:
            self._parse_state(state_response)
        
        return self._printer_data
    
    def _parse_temperature(self, response: bytes):
        """Parse temperature response"""
        match = TEMPERATURE_REPLY_REGEX.search(response)
        if match:
            self._printer_data["extruder_temp"] = float(match.group(1))
            self._printer_data["extruder_target"] = float(match.group(2))
            # T1 is second extruder (usually 0)
            # Bed temperature
            self._printer_data["bed_temp"] = float(match.group(5))
            self._printer_data["bed_target"] = float(match.group(6))
            logger.debug(f"[{self.printer_id}] Temp - Extruder: {self._printer_data['extruder_temp']}°C/{self._printer_data['extruder_target']}°C, Bed: {self._printer_data['bed_temp']}°C/{self._printer_data['bed_target']}°C")
    
    def _parse_state(self, response: bytes):
        """Parse state response"""
        match = STATE_REGEX.search(response)
        if match:
            machine_status = match.group(1).decode('utf-8', errors='ignore')
            move_mode = match.group(2).decode('utf-8', errors='ignore')
            status_flag = int(match.group(3))
            filename_bytes = match.group(6)
            filename = filename_bytes.decode('utf-8', errors='ignore').strip()
            
            self._printer_data["machine_status"] = machine_status
            self._printer_data["move_mode"] = move_mode
            self._printer_data["filename"] = filename
            
            # Map machine status to printer state
            old_state = self._printer_data["state"]
            if machine_status == 'BUILDING_FROM_SD' or machine_status == 'BUILDING_FROM_USB':
                self._set_state(PrinterState.PRINTING)
            elif machine_status == 'PAUSE_FROM_USER' or machine_status == 'PAUSE_FROM_GCODE':
                self._set_state(PrinterState.PAUSED)
            elif machine_status == 'IDLE' or machine_status == 'READY':
                self._set_state(PrinterState.READY)
            elif machine_status == 'FINISHED':
                self._set_state(PrinterState.COMPLETE)
            
            if old_state != self._printer_data["state"]:
                logger.info(f"[{self.printer_id}] State: {old_state} -> {self._printer_data['state']}")
            
            logger.debug(f"[{self.printer_id}] State: {machine_status}, File: {filename}")
    
    async def send_gcode(self, gcode: str) -> bool:
        """Send G-code command to printer"""
        if not self._connected:
            logger.warning(f"[{self.printer_id}] Not connected")
            return False
        
        command = f"{gcode}\r\n".encode()
        response = await self._send_command(command)
        if response and b'ok' in response.lower():
            logger.info(f"[{self.printer_id}] G-code '{gcode}' sent successfully")
            return True
        elif response:
            logger.warning(f"[{self.printer_id}] G-code '{gcode}' response: {response}")
            return True  # Still consider it sent
        return False
    
    async def pause_print(self) -> bool:
        """Pause printing"""
        return await self.send_gcode("~M25")
    
    async def resume_print(self) -> bool:
        """Resume printing"""
        return await self.send_gcode("~M24")
    
    async def cancel_print(self) -> bool:
        """Cancel printing"""
        return await self.send_gcode("~M0")


class DiscoveryService:
    """
    FlashForge Discovery Protocol Implementation
    
    Modern Protocol (5M / 5M Pro / AD5X):
    - UDP Multicast: 225.0.0.9:19000
    - UDP Broadcast: 255.255.255.255:48899
    - Response: 276 bytes binary packet
    
    Packet Structure (276 bytes, Big Endian):
    - Offset 0x00 (128 bytes): Machine Name (null-terminated)
    - Offset 0x84 (2 bytes): Command Port (TCP) - typically 8899
    - Offset 0x92 (128 bytes): Serial Number (null-terminated)
    - Offset 0x8E (2 bytes): HTTP/Event Port - typically 8898
    - Offset 0x8A (2 bytes): Status Code (0=Ready, 1=Busy, 2=Error)
    """
    
    def __init__(self):
        self.found_devices: Dict[str, Dict] = {}
        self._hosts_checked = 0
        self._printers_found = 0
    
    async def _send_udp_discovery(self, address: str, port: int, timeout: float = 2.0) -> List[Dict]:
        """Send UDP discovery packet and collect responses"""
        import socket
        
        found = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        
        try:
            # Send discovery packet (any payload, printer ignores it)
            discovery_packet = b"FLASHFORGE_DISCOVERY"
            sock.sendto(discovery_packet, (address, port))
            logger.debug(f"Sent UDP discovery to {address}:{port}")
            
            # Collect responses
            while True:
                try:
                    data, addr = sock.recvfrom(512)
                    ip = addr[0]
                    logger.debug(f"Received UDP response from {ip}: {len(data)} bytes")
                    
                    if len(data) >= 276:
                        # Parse response packet
                        printer_info = self._parse_discovery_response(data, ip)
                        if printer_info:
                            found.append(printer_info)
                            logger.info(f"UDP Discovery: {printer_info}")
                    else:
                        # Small response - just note the IP
                        found.append({
                            "valid": True,
                            "ip": ip,
                            "port": 8899,
                            "type": "flashforge_udp",
                            "info": {"message": "UDP discovery response"}
                        })
                        
                except socket.timeout:
                    break
                except Exception as e:
                    logger.debug(f"UDP response error: {e}")
                    break
                    
        finally:
            sock.close()
        
        return found
    
    def _parse_discovery_response(self, data: bytes, ip: str) -> Optional[Dict]:
        """Parse 276-byte discovery response"""
        if len(data) < 276:
            return None
        
        try:
            # Machine Name at offset 0x00 (128 bytes)
            machine_name = data[0x00:0x80].rstrip(b'\x00').decode('utf-8', errors='ignore')
            
            # Command Port (TCP) at offset 0x84 (2 bytes, Big Endian)
            command_port = int.from_bytes(data[0x84:0x86], byteorder='big')
            
            # Serial Number at offset 0x92 (128 bytes)
            serial_number = data[0x92:0x112].rstrip(b'\x00').decode('utf-8', errors='ignore')
            
            # HTTP/Event Port at offset 0x8E (2 bytes)
            http_port = int.from_bytes(data[0x8E:0x90], byteorder='big')
            
            # Status Code at offset 0x8A (2 bytes): 0=Ready, 1=Busy, 2=Error
            status_code = int.from_bytes(data[0x8A:0x8C], byteorder='big')
            status_map = {0: 'Ready', 1: 'Busy', 2: 'Error'}
            status = status_map.get(status_code, f'Unknown({status_code})')
            
            return {
                "valid": True,
                "ip": ip,
                "port": command_port if command_port > 0 else 8899,
                "type": "flashforge_discovery",
                "info": {
                    "machine_name": machine_name,
                    "serial_number": serial_number,
                    "http_port": http_port,
                    "status": status,
                    "status_code": status_code
                }
            }
        except Exception as e:
            logger.debug(f"Parse discovery response error: {e}")
            return None
    
    async def scan_subnet(self, subnet: str, port: int = 8899, max_hosts: int = MAX_PRINTERS) -> list:
        """Scan subnet using UDP discovery protocol"""
        from ipaddress import IPv4Network
        
        logger.info(f"Starting UDP discovery scan: subnet={subnet}")
        self._hosts_checked = 0
        self._printers_found = 0
        
        all_found = []
        
        # Method 1: UDP Multicast (225.0.0.9:19000)
        logger.info("Sending UDP Multicast discovery (225.0.0.9:19000)...")
        try:
            multicast_results = await self._send_udp_discovery("225.0.0.9", 19000)
            all_found.extend(multicast_results)
            logger.info(f"Multicast found: {len(multicast_results)} printers")
        except Exception as e:
            logger.debug(f"Multicast discovery error: {e}")
        
        # Method 2: UDP Broadcast (255.255.255.255:48899)
        logger.info("Sending UDP Broadcast discovery (255.255.255.255:48899)...")
        try:
            broadcast_results = await self._send_udp_discovery("255.255.255.255", 48899)
            all_found.extend(broadcast_results)
            logger.info(f"Broadcast found: {len(broadcast_results)} printers")
        except Exception as e:
            logger.debug(f"Broadcast discovery error: {e}")
        
        # Method 3: TCP port scan as fallback (for legacy printers)
        logger.info(f"TCP port scan fallback: subnet={subnet}, port={port}")
        tcp_results = await self._tcp_scan_subnet(subnet, port, max_hosts)
        all_found.extend(tcp_results)
        
        # Deduplicate by IP
        seen_ips = set()
        unique_found = []
        for device in all_found:
            ip = device.get('ip', '')
            if ip and ip not in seen_ips:
                seen_ips.add(ip)
                unique_found.append(device)
        
        self._printers_found = len(unique_found)
        logger.info(f"UDP Discovery complete: found {len(unique_found)} unique printers")
        return unique_found
    
    async def _tcp_scan_subnet(self, subnet: str, port: int, max_hosts: int) -> List[Dict]:
        """Fallback TCP port scan"""
        from ipaddress import IPv4Network
        
        try:
            network = IPv4Network(subnet, strict=False)
            hosts = [str(host) for host in network.hosts()][:max_hosts]
        except Exception as e:
            logger.error(f"Invalid subnet '{subnet}': {e}")
            return []
        
        semaphore = asyncio.Semaphore(50)
        
        async def check_host(ip: str):
            async with semaphore:
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, port),
                        timeout=1.0
                    )
                    writer.close()
                    await writer.wait_closed()
                    return {
                        "valid": True,
                        "ip": ip,
                        "port": port,
                        "type": "flashforge_tcp",
                        "info": {"message": f"TCP port {port} open"}
                    }
                except:
                    return None
        
        tasks = [check_host(ip) for ip in hosts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        found = [r for r in results if r is not None and isinstance(r, dict) and r.get("valid")]
        return found


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
                devices = await self.discovery.scan_subnet(subnet, port, 254)
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
            printer_port = data.get('printer_port', 8899)
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
        printer_port = int(os.environ.get('PRINTER_PORT', '8899'))
        
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
    
    async def _auto_discover_and_connect(self):
        """Auto-discover printers in network and connect to them"""
        subnet = os.environ.get('SCAN_SUBNET', '192.168.1.0/24')
        ports_str = os.environ.get('SCAN_PORTS', '8899')
        
        try:
            ports = [int(p.strip()) for p in ports_str.split(',') if p.strip()]
        except Exception as e:
            logger.warning(f"Failed to parse ports '{ports_str}': {e}")
            ports = [8899]
        
        logger.info(f"Starting auto-discovery: subnet={subnet}, ports={ports}")
        
        all_devices = []
        for port in ports:
            try:
                logger.info(f"Scanning port {port}...")
                devices = await self.discovery.scan_subnet(subnet, port, 254)
                all_devices.extend(devices)
            except Exception as e:
                logger.error(f"Discovery error on port {port}: {type(e).__name__}: {e}")
        
        if not all_devices:
            logger.warning(f"No printers found in {subnet}")
            return
        
        logger.info(f"Found {len(all_devices)} printer(s), connecting...")
        
        for device in all_devices:
            ip = device.get('ip', '')
            port = device.get('port', 8899)
            if not ip:
                continue
            
            printer_id = f"printer_{ip.replace('.', '_')}"
            if printer_id in self.printers:
                logger.debug(f"Printer {printer_id} already connected, skipping")
                continue
            
            logger.info(f"Connecting to discovered printer: {printer_id} at {ip}:{port}")
            new_printer = FlashforgeClient(printer_id, ip, port)
            self.printers[printer_id] = new_printer
            connected = await new_printer.connect()
            if connected:
                await new_printer.get_status()
                logger.info(f"Connected to discovered printer: {ip}:{port}")
            else:
                logger.warning(f"Failed to connect to discovered printer: {ip}:{port}")
        
        logger.info(f"Auto-discovery complete: {len(self.printers)} printer(s) connected")
    
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
        
        # Auto-connect to config printers OR auto-discover printers
        printer_ip = os.environ.get('PRINTER_IP', '')
        if printer_ip:
            logger.info("Printer IP configured - using manual configuration")
            await self._auto_connect_config_printers()
        else:
            logger.info("Printer IP not configured - starting auto-discovery...")
            await self._auto_discover_and_connect()
        
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
