#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pyzmq",
#   "websockets",
# ]
# ///
#
# bridge_server.py
from __future__ import annotations

import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import struct
import threading
import time
import uuid

MISSING_RUNTIME_DEPENDENCIES: list[str] = []

try:
    import zmq
    import zmq.asyncio
except ModuleNotFoundError:
    zmq = None
    MISSING_RUNTIME_DEPENDENCIES.append("pyzmq")

try:
    import websockets
except ModuleNotFoundError:
    websockets = None
    MISSING_RUNTIME_DEPENDENCIES.append("websockets")

# --- NetSync Protocol Constants ---
PROTOCOL_VERSION = 3
MSG_CLIENT_POSE = 11
MSG_ROOM_POSE = 12
MSG_RPC = 3
MSG_DEVICE_ID_MAPPING = 6
MSG_GLOBAL_VAR_SET = 7
MSG_GLOBAL_VAR_SYNC = 8
MSG_CLIENT_VAR_SET = 9
MSG_CLIENT_VAR_SYNC = 10

POSE_FLAG_STEALTH = 1 << 0
ENCODING_FLAGS_DEFAULT = 0x1F
DISCOVERY_REQUEST = "STYLY-NETSYNC-DISCOVER"
DISCOVERY_RESPONSE_PREFIX = "STYLY-NETSYNC"
DEFAULT_SERVER_DISCOVERY_PORT = 9999
DEFAULT_HTTP_PORT = 8080
WEB_CLIENT_FILENAME = "NetSyncWebClient.html"


def ensure_runtime_dependencies():
    if not MISSING_RUNTIME_DEPENDENCIES:
        return

    missing = ", ".join(MISSING_RUNTIME_DEPENDENCIES)
    print(
        "[Bridge] Missing Python packages: "
        f"{missing}. Install with `python3 -m pip install {' '.join(MISSING_RUNTIME_DEPENDENCIES)}`"
    )
    raise SystemExit(1)

# --- Binary Protocol Helpers ---

def pack_string(buf: bytearray, s: str, use_ushort=False):
    b = s.encode("utf-8")
    if use_ushort:
        buf.extend(struct.pack("<H", len(b)))
    else:
        buf.append(len(b))
    buf.extend(b)

def unpack_string(data: bytes, offset: int, use_ushort=False):
    if use_ushort:
        length = struct.unpack("<H", data[offset:offset+2])[0]
        offset += 2
    else:
        length = data[offset]
        offset += 1
    s = data[offset:offset+length].decode("utf-8")
    return s, offset + length

def serialize_stealth_handshake(device_id: str) -> bytes:
    buf = bytearray()
    buf.append(MSG_CLIENT_POSE)
    buf.append(PROTOCOL_VERSION)
    pack_string(buf, device_id)
    buf.extend(struct.pack("<H", 0))
    buf.append(POSE_FLAG_STEALTH)
    buf.append(ENCODING_FLAGS_DEFAULT)
    buf.append(0)
    return bytes(buf)

def serialize_rpc(function_name: str, args: list[str],
                  sender_client_no: int = 0,
                  target_client_nos: list[int] = None) -> bytes:
    buf = bytearray()
    buf.append(MSG_RPC)
    buf.extend(struct.pack("<H", sender_client_no))
    targets = target_client_nos or []
    buf.append(len(targets))
    for t in targets:
        buf.extend(struct.pack("<H", t))
    pack_string(buf, function_name)
    args_json = json.dumps(args)
    pack_string(buf, args_json, use_ushort=True)
    return bytes(buf)

def serialize_global_var_set(sender_client_no: int, name: str, value: str) -> bytes:
    buf = bytearray()
    buf.append(MSG_GLOBAL_VAR_SET)
    buf.extend(struct.pack("<H", sender_client_no))
    pack_string(buf, name[:64])
    pack_string(buf, value[:1024], use_ushort=True)
    buf.extend(struct.pack("<d", time.time()))
    return bytes(buf)

def serialize_client_var_set(sender_client_no: int, target_client_no: int,
                             name: str, value: str) -> bytes:
    buf = bytearray()
    buf.append(MSG_CLIENT_VAR_SET)
    buf.extend(struct.pack("<H", sender_client_no))
    buf.extend(struct.pack("<H", target_client_no))
    pack_string(buf, name[:64])
    pack_string(buf, value[:1024], use_ushort=True)
    buf.extend(struct.pack("<d", time.time()))
    return bytes(buf)

# --- Deserialization ---

def unpack_int24_le(data: bytes, offset: int):
    raw = data[offset] | (data[offset+1] << 8) | (data[offset+2] << 16)
    offset += 3
    if raw & 0x800000:
        raw -= 1 << 24
    return raw, offset

def deserialize_room_pose(data: bytes) -> dict:
    offset = 0
    msg_type = data[offset]; offset += 1
    proto = data[offset]; offset += 1
    room_id, offset = unpack_string(data, offset)
    broadcast_time = struct.unpack("<d", data[offset:offset+8])[0]; offset += 8
    client_count = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2

    clients = []
    for _ in range(client_count):
        client_no = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
        pose_time = struct.unpack("<d", data[offset:offset+8])[0]; offset += 8
        pose_seq = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
        flags = data[offset]; offset += 1
        enc_flags = data[offset]; offset += 1

        client = {"clientNo": client_no, "poseTime": pose_time, "flags": flags}

        phys_valid = bool(flags & (1 << 1))
        head_valid = bool(flags & (1 << 2))
        right_valid = head_valid and bool(flags & (1 << 3))
        left_valid = head_valid and bool(flags & (1 << 4))
        virt_valid = head_valid and bool(flags & (1 << 5))

        if phys_valid:
            dx, dz, dyaw = struct.unpack("<hhh", data[offset:offset+6]); offset += 6
            client["xrOriginDelta"] = {"x": dx*0.01, "z": dz*0.01, "yaw": dyaw*0.1}

        if head_valid:
            hx, offset = unpack_int24_le(data, offset)
            hy, offset = unpack_int24_le(data, offset)
            hz, offset = unpack_int24_le(data, offset)
            head_rot_packed = struct.unpack("<I", data[offset:offset+4])[0]; offset += 4
            client["head"] = {"pos": {"x": hx*0.01, "y": hy*0.01, "z": hz*0.01}, "rotPacked": head_rot_packed}

        if right_valid:
            rx, ry, rz = struct.unpack("<hhh", data[offset:offset+6]); offset += 6
            rrot = struct.unpack("<I", data[offset:offset+4])[0]; offset += 4
            client["right"] = {"relPos": {"x": rx*0.005, "y": ry*0.005, "z": rz*0.005}, "rotPacked": rrot}

        if left_valid:
            lx, ly, lz = struct.unpack("<hhh", data[offset:offset+6]); offset += 6
            lrot = struct.unpack("<I", data[offset:offset+4])[0]; offset += 4
            client["left"] = {"relPos": {"x": lx*0.005, "y": ly*0.005, "z": lz*0.005}, "rotPacked": lrot}

        v_count = data[offset]; offset += 1
        if virt_valid and v_count > 0:
            virtuals = []
            for _ in range(v_count):
                vx, vy, vz = struct.unpack("<hhh", data[offset:offset+6]); offset += 6
                vrot = struct.unpack("<I", data[offset:offset+4])[0]; offset += 4
                virtuals.append({"relPos": {"x": vx*0.005, "y": vy*0.005, "z": vz*0.005}, "rotPacked": vrot})
            client["virtuals"] = virtuals
        else:
            for _ in range(v_count):
                offset += 10

        clients.append(client)

    return {"type": "room_pose", "roomId": room_id, "broadcastTime": broadcast_time, "clients": clients}

def deserialize_rpc(data: bytes) -> dict:
    offset = 1
    sender = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
    target_count = data[offset]; offset += 1
    targets = []
    for _ in range(target_count):
        targets.append(struct.unpack("<H", data[offset:offset+2])[0]); offset += 2
    func_name, offset = unpack_string(data, offset)
    args_json, offset = unpack_string(data, offset, use_ushort=True)
    return {"type": "rpc", "senderClientNo": sender, "targetClientNos": targets,
            "functionName": func_name, "args": json.loads(args_json) if args_json else []}

def deserialize_id_mapping(data: bytes) -> dict:
    offset = 1
    ver_major = data[offset]; offset += 1
    ver_minor = data[offset]; offset += 1
    ver_patch = data[offset]; offset += 1
    count = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
    mappings = []
    for _ in range(count):
        cno = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
        is_stealth = data[offset] == 0x01; offset += 1
        dev_id, offset = unpack_string(data, offset)
        mappings.append({"clientNo": cno, "deviceId": dev_id, "stealth": is_stealth})
    return {"type": "id_mapping", "serverVersion": f"{ver_major}.{ver_minor}.{ver_patch}", "mappings": mappings}

def deserialize_global_var_sync(data: bytes) -> dict:
    offset = 1
    count = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
    variables = []
    for _ in range(count):
        name, offset = unpack_string(data, offset)
        value, offset = unpack_string(data, offset, use_ushort=True)
        ts = struct.unpack("<d", data[offset:offset+8])[0]; offset += 8
        writer = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
        variables.append({"name": name, "value": value, "timestamp": ts, "lastWriter": writer})
    return {"type": "global_var_sync", "variables": variables}

def deserialize_client_var_sync(data: bytes) -> dict:
    offset = 1
    client_count = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
    client_vars = {}
    for _ in range(client_count):
        cno = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
        var_count = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
        variables = []
        for _ in range(var_count):
            name, offset = unpack_string(data, offset)
            value, offset = unpack_string(data, offset, use_ushort=True)
            ts = struct.unpack("<d", data[offset:offset+8])[0]; offset += 8
            writer = struct.unpack("<H", data[offset:offset+2])[0]; offset += 2
            variables.append({"name": name, "value": value, "timestamp": ts, "lastWriter": writer})
        client_vars[str(cno)] = variables
    return {"type": "client_var_sync", "clientVariables": client_vars}

def deserialize_sub_message(topic: bytes, payload: bytes) -> dict | None:
    if not payload:
        return None
    msg_type = payload[0]
    try:
        if msg_type == MSG_ROOM_POSE:
            return deserialize_room_pose(payload)
        elif msg_type == MSG_RPC:
            return deserialize_rpc(payload)
        elif msg_type == MSG_DEVICE_ID_MAPPING:
            return deserialize_id_mapping(payload)
        elif msg_type == MSG_GLOBAL_VAR_SYNC:
            return deserialize_global_var_sync(payload)
        elif msg_type == MSG_CLIENT_VAR_SYNC:
            return deserialize_client_var_sync(payload)
    except Exception as e:
        print(f"Deserialize error (type={msg_type}): {e}")
    return None


def get_lan_ipv4_candidates() -> list[str]:
    candidates: set[str] = set()

    try:
        hostname = socket.gethostname()
        for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None, socket.AF_INET):
            if family == socket.AF_INET:
                ip = sockaddr[0]
                if not ip.startswith("127."):
                    candidates.add(ip)
    except socket.gaierror:
        pass

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        if ip and not ip.startswith("127."):
            candidates.add(ip)
        probe.close()
    except OSError:
        pass

    return sorted(candidates)


def get_broadcast_addresses(local_ips: list[str]) -> list[str]:
    addresses = {"255.255.255.255"}

    for ip in local_ips:
        parts = ip.split(".")
        if len(parts) == 4:
            addresses.add(".".join(parts[:3] + ["255"]))

    return sorted(addresses)


def parse_discovery_response(data: bytes, sender_ip: str) -> dict | None:
    try:
        message = data.decode("utf-8").strip()
        parts = message.split("|")
        if len(parts) < 3 or parts[0] != DISCOVERY_RESPONSE_PREFIX:
            return None

        dealer_port = int(parts[1])
        sub_port = int(parts[2])
        server_name = parts[3] if len(parts) >= 4 and parts[3] else "Unknown Server"

        return {
            "serverAddress": f"tcp://{sender_ip}",
            "dealerPort": dealer_port,
            "subPort": sub_port,
            "serverName": server_name,
        }
    except (UnicodeDecodeError, ValueError):
        return None


def try_tcp_discovery(ip_address: str, discovery_port: int, timeout_sec: float) -> dict | None:
    try:
        with socket.create_connection((ip_address, discovery_port), timeout=timeout_sec) as sock:
            sock.settimeout(timeout_sec)
            sock.sendall(DISCOVERY_REQUEST.encode("utf-8"))
            response = sock.recv(1024)
            if not response:
                return None
            return parse_discovery_response(response, ip_address)
    except OSError:
        return None


def discover_netsync_server(discovery_port: int = DEFAULT_SERVER_DISCOVERY_PORT,
                            timeout_sec: float = 3.0,
                            interval_sec: float = 0.25) -> dict | None:
    localhost_result = try_tcp_discovery("127.0.0.1", discovery_port, min(timeout_sec, 0.5))
    if localhost_result:
        localhost_result["discoveryMethod"] = "tcp-localhost"
        return localhost_result

    local_ips = get_lan_ipv4_candidates()
    broadcast_addresses = get_broadcast_addresses(local_ips)
    request = DISCOVERY_REQUEST.encode("utf-8")
    sockets: list[socket.socket] = []

    try:
        for ip in local_ips:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(0.2)
                sock.bind((ip, 0))
                sockets.append(sock)
            except OSError:
                pass

        if not sockets:
            fallback = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            fallback.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            fallback.settimeout(0.2)
            fallback.bind(("", 0))
            sockets.append(fallback)

        deadline = time.monotonic() + timeout_sec
        next_send_at = 0.0

        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send_at:
                for sock in sockets:
                    for broadcast_ip in broadcast_addresses:
                        try:
                            sock.sendto(request, (broadcast_ip, discovery_port))
                        except OSError:
                            pass
                next_send_at = now + interval_sec

            remaining = max(0.0, deadline - time.monotonic())
            poll_timeout = min(0.2, remaining)
            if poll_timeout <= 0:
                break

            for sock in sockets:
                try:
                    sock.settimeout(poll_timeout)
                    response, sender = sock.recvfrom(1024)
                    result = parse_discovery_response(response, sender[0])
                    if result:
                        result["discoveryMethod"] = "udp-broadcast"
                        return result
                except socket.timeout:
                    continue
                except OSError:
                    continue

        return None
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass


class QuietWebClientHandler(SimpleHTTPRequestHandler):
    """Serve the console HTML without noisy per-request access logs."""

    def log_message(self, format: str, *args) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self) -> None:
        if self.path in {"", "/"}:
            self.path = f"/{WEB_CLIENT_FILENAME}"
        super().do_GET()


class WebClientHttpServer:
    def __init__(self, host: str, port: int, directory: Path) -> None:
        self.host = host
        self.port = port
        self.directory = directory
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = partial(QuietWebClientHandler, directory=str(self.directory))
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._print_urls()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _print_urls(self) -> None:
        port = self._server.server_port if self._server is not None else self.port
        if self.host in {"0.0.0.0", "::", ""}:
            candidates = [f"http://127.0.0.1:{port}/"]
            candidates.extend(f"http://{ip}:{port}/" for ip in get_lan_ipv4_candidates())
            print("[HTTP] Web console URLs:")
            for url in candidates:
                print(f"  - {url}")
        else:
            print(f"[HTTP] Web console URL: http://{self.host}:{port}/")


# --- Bridge Core ---

class WebBridge:
    def __init__(self, server_address="tcp://localhost",
                 dealer_port=5555, sub_port=5556, room_id="default_room"):
        self.server_address = server_address
        self.dealer_port = dealer_port
        self.sub_port = sub_port
        self.room_id = room_id
        self.server_name = "Unknown Server"
        self.discovery_method = None
        self.ws_clients: dict[websockets.WebSocketServerProtocol, str] = {}
        self.ctx = zmq.asyncio.Context()

        # ★ Network Variable キャッシュ
        self.global_variables: dict[str, dict] = {}       # name -> {value, timestamp, lastWriter}
        self.client_variables: dict[str, dict[str, dict]] = {}  # clientNo(str) -> {name -> {value, timestamp, lastWriter}}
        self.id_mappings: list[dict] = []
        self.room_pose_clients: dict[str, dict] = {}

    def _update_nv_cache(self, msg: dict):
        """SUBメッセージからNVキャッシュを更新"""
        if msg["type"] == "room_pose":
            self.room_pose_clients = {str(client["clientNo"]): client for client in msg.get("clients", [])}
        elif msg["type"] == "global_var_sync":
            for v in msg["variables"]:
                self.global_variables[v["name"]] = {
                    "value": v["value"],
                    "timestamp": v["timestamp"],
                    "lastWriter": v["lastWriter"]
                }
        elif msg["type"] == "client_var_sync":
            for cno_str, variables in msg["clientVariables"].items():
                if cno_str not in self.client_variables:
                    self.client_variables[cno_str] = {}
                for v in variables:
                    self.client_variables[cno_str][v["name"]] = {
                        "value": v["value"],
                        "timestamp": v["timestamp"],
                        "lastWriter": v["lastWriter"]
                    }
        elif msg["type"] == "id_mapping":
            self.id_mappings = msg["mappings"]

    def _build_room_summary(self) -> dict:
        mapped_clients = [m for m in self.id_mappings if not m.get("stealth")]
        stealth_clients = [m for m in self.id_mappings if m.get("stealth")]
        return {
            "roomClientCount": len(mapped_clients),
            "poseClientCount": len(self.room_pose_clients),
            "stealthClientCount": len(stealth_clients),
            "webClientCount": len(self.ws_clients),
            "maxRecommendedClients": 100
        }

    def _build_snapshot(self) -> str:
        """現在のNV状態をまとめたJSONを返す"""
        return json.dumps({
            "type": "nv_snapshot",
            "globalVariables": self.global_variables,
            "clientVariables": self.client_variables,
            "idMappings": self.id_mappings,
            "roomSummary": self._build_room_summary()
        })

    def _build_bridge_status(self) -> str:
        return json.dumps({
            "type": "bridge_status",
            "roomSummary": self._build_room_summary(),
            "netSync": {
                "serverAddress": self.server_address,
                "dealerPort": self.dealer_port,
                "subPort": self.sub_port,
                "serverName": self.server_name,
                "discoveryMethod": self.discovery_method,
            }
        })

    def configure_discovered_server(self, discovered: dict):
        self.server_address = discovered["serverAddress"]
        self.dealer_port = discovered["dealerPort"]
        self.sub_port = discovered["subPort"]
        self.server_name = discovered.get("serverName", "Unknown Server")
        self.discovery_method = discovered.get("discoveryMethod")

    def ensure_server_endpoint(self,
                               discover: bool = False,
                               discovery_port: int = DEFAULT_SERVER_DISCOVERY_PORT,
                               discovery_timeout: float = 3.0):
        should_discover = discover or not self.server_address or self.server_address in {"auto", "discover"}
        if not should_discover:
            print(
                f"[Bridge] Using NetSync server {self.server_address} "
                f"(dealer:{self.dealer_port}, sub:{self.sub_port})"
            )
            return

        print(f"[Bridge] Discovering NetSync server on port {discovery_port}...")
        discovered = discover_netsync_server(
            discovery_port=discovery_port,
            timeout_sec=discovery_timeout,
        )
        if not discovered:
            raise RuntimeError(
                "NetSync server discovery failed. "
                "Specify --server tcp://<host> or use a reachable discovery port."
            )

        self.configure_discovered_server(discovered)
        print(
            f"[Bridge] Discovered NetSync server '{self.server_name}' at {self.server_address} "
            f"(dealer:{self.dealer_port}, sub:{self.sub_port}, via {self.discovery_method})"
        )

    async def _broadcast_bridge_status(self):
        if not self.ws_clients:
            return

        json_str = self._build_bridge_status()
        disconnected = []
        for ws in list(self.ws_clients.keys()):
            try:
                await ws.send(json_str)
            except websockets.ConnectionClosed:
                disconnected.append(ws)
        for ws in disconnected:
            del self.ws_clients[ws]

    async def zmq_subscriber(self):
        sub = self.ctx.socket(zmq.SUB)
        sub.connect(f"{self.server_address}:{self.sub_port}")
        sub.subscribe(self.room_id.encode("utf-8"))
        print(f"[SUB] Subscribed to room: {self.room_id}")

        while True:
            try:
                parts = await sub.recv_multipart()
                if len(parts) >= 2:
                    topic, payload = parts[0], parts[1]
                    msg = deserialize_sub_message(topic, payload)
                    if msg:
                        # ★ NVキャッシュを更新
                        self._update_nv_cache(msg)

                        json_str = json.dumps(msg)
                        disconnected = []
                        for ws in list(self.ws_clients.keys()):
                            try:
                                await ws.send(json_str)
                            except websockets.ConnectionClosed:
                                disconnected.append(ws)
                        for ws in disconnected:
                            del self.ws_clients[ws]
            except Exception as e:
                print(f"[SUB] Error: {e}")
                await asyncio.sleep(0.1)

    async def zmq_dealer_receiver(self, dealer, ws, device_id):
        """DEALER→ROUTER経由で受信するコントロールメッセージを処理"""
        while ws in self.ws_clients:
            try:
                parts = await dealer.recv_multipart(flags=zmq.NOBLOCK)
                if len(parts) >= 2:
                    room_bytes, payload = parts[0], parts[1]
                    msg = deserialize_sub_message(room_bytes, payload)
                    if msg:
                        self._update_nv_cache(msg)
                        await ws.send(json.dumps(msg))
            except zmq.Again:
                await asyncio.sleep(0.05)
            except Exception as e:
                print(f"[DEALER-RX] Error: {e}")
                await asyncio.sleep(0.1)

    async def handle_ws_client(self, ws):
        device_id = f"web-{uuid.uuid4().hex[:12]}"
        self.ws_clients[ws] = device_id
        print(f"[WS] Client connected: {device_id}")
        await self._broadcast_bridge_status()

        dealer = self.ctx.socket(zmq.DEALER)
        dealer.setsockopt(zmq.LINGER, 0)
        dealer.connect(f"{self.server_address}:{self.dealer_port}")

        handshake = serialize_stealth_handshake(device_id)
        await dealer.send_multipart([self.room_id.encode("utf-8"), handshake])
        print(f"[DEALER] Sent stealth handshake for {device_id}")

        # ★ 接続時に現在のNVスナップショットを送信
        await ws.send(self._build_snapshot())

        async def keepalive():
            while ws in self.ws_clients:
                await asyncio.sleep(2.0)
                try:
                    hs = serialize_stealth_handshake(device_id)
                    await dealer.send_multipart([self.room_id.encode("utf-8"), hs])
                except Exception:
                    break

        keepalive_task = asyncio.create_task(keepalive())
        dealer_rx_task = asyncio.create_task(self.zmq_dealer_receiver(dealer, ws, device_id))

        try:
            async for raw_msg in ws:
                msg = json.loads(raw_msg)
                action = msg.get("action")
                room = self.room_id.encode("utf-8")

                if action == "rpc":
                    payload = serialize_rpc(
                        msg["functionName"],
                        msg.get("args", []),
                        msg.get("senderClientNo", 0),
                        msg.get("targetClientNos")
                    )
                    await dealer.send_multipart([room, payload])

                elif action == "set_global_var":
                    payload = serialize_global_var_set(
                        msg.get("senderClientNo", 0),
                        msg["name"], msg["value"]
                    )
                    await dealer.send_multipart([room, payload])

                elif action == "set_client_var":
                    payload = serialize_client_var_set(
                        msg.get("senderClientNo", 0),
                        msg["targetClientNo"],
                        msg["name"], msg["value"]
                    )
                    await dealer.send_multipart([room, payload])

                elif action == "get_snapshot":
                    # ★ クライアントからのスナップショットリクエスト
                    await ws.send(self._build_snapshot())

        except websockets.ConnectionClosed:
            pass
        finally:
            keepalive_task.cancel()
            dealer_rx_task.cancel()
            dealer.close()
            if ws in self.ws_clients:
                del self.ws_clients[ws]
            await self._broadcast_bridge_status()
            print(f"[WS] Client disconnected: {device_id}")

    async def run(self, ws_host="0.0.0.0", ws_port=8765,
                  discover=False,
                  discovery_port=DEFAULT_SERVER_DISCOVERY_PORT,
                  discovery_timeout=3.0,
                  http_host="0.0.0.0",
                  http_port=DEFAULT_HTTP_PORT,
                  http_enabled=True):
        self.ensure_server_endpoint(
            discover=discover,
            discovery_port=discovery_port,
            discovery_timeout=discovery_timeout,
        )
        http_server = None
        if http_enabled:
            web_root = Path(__file__).resolve().parent
            web_client = web_root / WEB_CLIENT_FILENAME
            if web_client.exists():
                http_server = WebClientHttpServer(http_host, http_port, web_root)
                try:
                    http_server.start()
                except OSError as exc:
                    http_server = None
                    print(f"[HTTP] Skipped. Failed to bind {http_host}:{http_port}: {exc}")
            else:
                print(f"[HTTP] Skipped. Missing {web_client}")

        print(f"[Bridge] Starting on ws://{ws_host}:{ws_port}")
        if ws_host in {"0.0.0.0", "::", ""}:
            candidates = [f"ws://127.0.0.1:{ws_port}"]
            candidates.extend(f"ws://{ip}:{ws_port}" for ip in get_lan_ipv4_candidates())
            if candidates:
                print("[Bridge] Reachable URLs:")
                for url in candidates:
                    print(f"  - {url}")
        else:
            print(f"[Bridge] Reachable URL: ws://{ws_host}:{ws_port}")
        asyncio.create_task(self.zmq_subscriber())
        try:
            async with websockets.serve(self.handle_ws_client, ws_host, ws_port):
                await asyncio.Future()
        finally:
            if http_server is not None:
                http_server.stop()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="auto")
    parser.add_argument("--dealer-port", type=int, default=5555)
    parser.add_argument("--sub-port", type=int, default=5556)
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--server-discovery-port", type=int, default=DEFAULT_SERVER_DISCOVERY_PORT)
    parser.add_argument("--discovery-timeout", type=float, default=3.0)
    parser.add_argument("--room", default="default_room")
    parser.add_argument("--ws-host", default="0.0.0.0")
    parser.add_argument("--ws-port", type=int, default=8765)
    parser.add_argument("--http-host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--no-http", action="store_true")
    args = parser.parse_args()

    ensure_runtime_dependencies()

    bridge = WebBridge(args.server, args.dealer_port, args.sub_port, args.room)
    try:
        asyncio.run(
            bridge.run(
                ws_host=args.ws_host,
                ws_port=args.ws_port,
                discover=args.discover,
                discovery_port=args.server_discovery_port,
                discovery_timeout=args.discovery_timeout,
                http_host=args.http_host,
                http_port=args.http_port,
                http_enabled=not args.no_http,
            )
        )
    except RuntimeError as exc:
        print(f"[Bridge] {exc}")
        raise SystemExit(1)
