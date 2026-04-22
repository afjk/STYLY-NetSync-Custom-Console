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
from contextlib import suppress
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import random
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
POSE_FLAG_PHYSICAL_VALID = 1 << 1
POSE_FLAG_HEAD_VALID = 1 << 2
POSE_FLAG_RIGHT_VALID = 1 << 3
POSE_FLAG_LEFT_VALID = 1 << 4
POSE_FLAG_VIRTUALS_VALID = 1 << 5
ENCODING_FLAGS_DEFAULT = 0x1F
DISCOVERY_REQUEST = "STYLY-NETSYNC-DISCOVER"
DISCOVERY_RESPONSE_PREFIX = "STYLY-NETSYNC"
DEFAULT_SERVER_DISCOVERY_PORT = 9999
DEFAULT_HTTP_PORT = 8080
WEB_CLIENT_FILENAME = "NetSyncWebClient.html"
DUMMY_SEND_INTERVAL_SEC = 0.1
DUMMY_HEAD_POS_SCALE = 0.01
DUMMY_REL_POS_SCALE = 0.005
DUMMY_PHYSICAL_DELTA_SCALE = 0.01
DUMMY_YAW_SCALE = 0.1


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


def euler_to_quat(yaw_rad: float) -> tuple[float, float, float, float]:
    half = yaw_rad * 0.5
    return 0.0, math.sin(half), 0.0, math.cos(half)


def quantize_s16(value: float, scale: float) -> int:
    quantized = int(round(value / scale))
    return max(-(1 << 15), min((1 << 15) - 1, quantized))


def quantize_s24(value: float, scale: float) -> int:
    quantized = int(round(value / scale))
    return max(-(1 << 23), min((1 << 23) - 1, quantized))


def pack_int24_le(buf: bytearray, value: int) -> None:
    raw = value & 0xFFFFFF
    buf.extend((raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF))


def compress_quat_smallest_three(qx: float, qy: float, qz: float, qw: float) -> int:
    components = [qx, qy, qz, qw]
    largest_index = max(range(4), key=lambda index: abs(components[index]))
    if components[largest_index] < 0.0:
        components = [-value for value in components]

    max_component = 1.0 / math.sqrt(2.0)
    packed = largest_index << 30
    remaining = [components[index] for index in range(4) if index != largest_index]
    for shift, value in zip((20, 10, 0), remaining):
        clamped = max(-max_component, min(max_component, value))
        normalized = (clamped + max_component) / (2.0 * max_component)
        packed |= int(round(normalized * 1023.0)) << shift
    return packed


def serialize_avatar_transform(device_id: str,
                               head_pos: tuple[float, float, float],
                               head_yaw_rad: float,
                               right_rel: tuple[float, float, float],
                               left_rel: tuple[float, float, float],
                               physical_pos: tuple[float, float, float] | None,
                               virtuals: list[tuple[float, float, float]] | None,
                               pose_seq: int) -> bytes:
    flags = POSE_FLAG_HEAD_VALID | POSE_FLAG_RIGHT_VALID | POSE_FLAG_LEFT_VALID
    virtual_positions = list(virtuals or [])
    if physical_pos is not None:
        flags |= POSE_FLAG_PHYSICAL_VALID
    if virtual_positions:
        flags |= POSE_FLAG_VIRTUALS_VALID

    buf = bytearray()
    buf.append(MSG_CLIENT_POSE)
    buf.append(PROTOCOL_VERSION)
    pack_string(buf, device_id)
    buf.extend(struct.pack("<H", pose_seq & 0xFFFF))
    buf.append(flags)
    buf.append(ENCODING_FLAGS_DEFAULT)

    identity_rot = compress_quat_smallest_three(0.0, 0.0, 0.0, 1.0)
    if physical_pos is not None:
        delta_x = head_pos[0] - physical_pos[0]
        delta_z = head_pos[2] - physical_pos[2]
        buf.extend(struct.pack(
            "<hhh",
            quantize_s16(delta_x, DUMMY_PHYSICAL_DELTA_SCALE),
            quantize_s16(delta_z, DUMMY_PHYSICAL_DELTA_SCALE),
            quantize_s16(0.0, DUMMY_YAW_SCALE),
        ))

    for axis in head_pos:
        pack_int24_le(buf, quantize_s24(axis, DUMMY_HEAD_POS_SCALE))
    buf.extend(struct.pack("<I", compress_quat_smallest_three(*euler_to_quat(head_yaw_rad))))

    for rel_pos in (right_rel, left_rel):
        buf.extend(struct.pack(
            "<hhhI",
            quantize_s16(rel_pos[0], DUMMY_REL_POS_SCALE),
            quantize_s16(rel_pos[1], DUMMY_REL_POS_SCALE),
            quantize_s16(rel_pos[2], DUMMY_REL_POS_SCALE),
            identity_rot,
        ))

    buf.append(len(virtual_positions))
    for virtual_pos in virtual_positions:
        rel_x = virtual_pos[0] - head_pos[0]
        rel_y = virtual_pos[1] - head_pos[1]
        rel_z = virtual_pos[2] - head_pos[2]
        buf.extend(struct.pack(
            "<hhhI",
            quantize_s16(rel_x, DUMMY_REL_POS_SCALE),
            quantize_s16(rel_y, DUMMY_REL_POS_SCALE),
            quantize_s16(rel_z, DUMMY_REL_POS_SCALE),
            identity_rot,
        ))

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

        if phys_valid and head_valid:
            delta = client.get("xrOriginDelta", {})
            d_x = delta.get("x", 0.0)
            d_z = delta.get("z", 0.0)
            d_yaw = delta.get("yaw", 0.0)
            hp = client["head"]["pos"]
            yaw_rad = math.radians(-d_yaw)
            tx = hp["x"] - d_x
            tz = hp["z"] - d_z
            cos_y = math.cos(yaw_rad)
            sin_y = math.sin(yaw_rad)
            px = cos_y * tx + sin_y * tz
            pz = -sin_y * tx + cos_y * tz
            client["physical"] = {"pos": {"x": px, "y": hp["y"], "z": pz}}

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

def wrap_angle_rad(angle: float) -> float:
    while angle > math.pi:
        angle -= math.tau
    while angle < -math.pi:
        angle += math.tau
    return angle


class DummyAvatar:
    def __init__(self, bridge: "WebBridge", start_x: float, start_z: float) -> None:
        self.bridge = bridge
        self.device_id = f"dummy-{uuid.uuid4().hex[:12]}"
        self.socket = bridge.ctx.socket(zmq.DEALER)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.connect(f"{bridge.server_address}:{bridge.dealer_port}")
        self.pose_seq = 0
        self.targets: list[tuple[float, float]] = []
        self.phys_x = start_x
        self.phys_z = start_z
        self.loco_x = 0.0
        self.loco_z = 0.0
        self.head_yaw_rad = random.uniform(-math.pi, math.pi)
        self.move_speed = 1.4 * random.uniform(0.85, 1.15)
        self.head_height = random.uniform(1.55, 1.75)
        self.hand_phase = random.uniform(0.0, math.tau)
        self.virtual_orbits = [
            {
                "radius": random.uniform(0.22, 0.55),
                "speed": random.uniform(0.6, 1.7),
                "height": random.uniform(-0.15, 0.3),
                "phase": random.uniform(0.0, math.tau),
            }
            for _ in range(random.randint(2, 3))
        ]

    def close(self) -> None:
        self.socket.close()

    def set_target(self, x: float, z: float) -> None:
        self.targets = [(x, z)]

    def add_target(self, x: float, z: float) -> None:
        self.targets.append((x, z))

    def update(self, dt: float) -> None:
        moving = False
        if self.targets:
            target_x, target_z = self.targets[0]
            delta_x = target_x - self.phys_x
            delta_z = target_z - self.phys_z
            distance = math.hypot(delta_x, delta_z)
            if distance < 0.1:
                self.phys_x = target_x
                self.phys_z = target_z
                self.targets.pop(0)
            elif distance > 0.0:
                moving = True
                desired_yaw = math.atan2(delta_x, delta_z)
                yaw_delta = wrap_angle_rad(desired_yaw - self.head_yaw_rad)
                max_turn = 5.0 * dt
                self.head_yaw_rad = wrap_angle_rad(
                    self.head_yaw_rad + max(-max_turn, min(max_turn, yaw_delta))
                )
                step = min(self.move_speed * dt, distance)
                self.phys_x += (delta_x / distance) * step
                self.phys_z += (delta_z / distance) * step

        if not moving and self.targets:
            target_x, target_z = self.targets[0]
            delta_x = target_x - self.phys_x
            delta_z = target_z - self.phys_z
            if math.hypot(delta_x, delta_z) > 1e-4:
                desired_yaw = math.atan2(delta_x, delta_z)
                yaw_delta = wrap_angle_rad(desired_yaw - self.head_yaw_rad)
                max_turn = 5.0 * dt
                self.head_yaw_rad = wrap_angle_rad(
                    self.head_yaw_rad + max(-max_turn, min(max_turn, yaw_delta))
                )

        forward_x = math.sin(self.head_yaw_rad)
        forward_z = math.cos(self.head_yaw_rad)
        target_loco_x = forward_x * (0.09 if moving else 0.0)
        target_loco_z = forward_z * (0.09 if moving else 0.0)
        blend = min(1.0, dt * 6.0)
        self.loco_x += (target_loco_x - self.loco_x) * blend
        self.loco_z += (target_loco_z - self.loco_z) * blend

    def get_transform(self, now: float) -> dict:
        moving = bool(self.targets)
        amplitude = 0.2 if moving else 0.03
        frequency = 4.0 if moving else 0.8
        swing = amplitude * math.sin((math.tau * frequency * now) + self.hand_phase)
        lift = abs(swing) * 0.15

        head_x = self.phys_x + self.loco_x
        head_y = self.head_height
        head_z = self.phys_z + self.loco_z
        head_pos = (head_x, head_y, head_z)

        right_rel = (0.24, -0.34 + lift, 0.18 + swing)
        left_rel = (-0.24, -0.34 + lift, 0.18 - swing)

        virtuals = []
        for orbit in self.virtual_orbits:
            angle = (now * orbit["speed"]) + orbit["phase"]
            virtuals.append((
                head_x + math.cos(angle) * orbit["radius"],
                head_y + orbit["height"] + (0.05 * math.sin(angle * 1.7)),
                head_z + math.sin(angle) * orbit["radius"],
            ))

        result = {
            "device_id": self.device_id,
            "head_pos": head_pos,
            "head_yaw_rad": self.head_yaw_rad,
            "right_rel": right_rel,
            "left_rel": left_rel,
            "physical_pos": (self.phys_x, 0.0, self.phys_z),
            "virtuals": virtuals,
            "pose_seq": self.pose_seq,
        }
        self.pose_seq = (self.pose_seq + 1) & 0xFFFF
        return result


class DummyAvatarManager:
    def __init__(self, bridge: "WebBridge") -> None:
        self.bridge = bridge
        self.avatars: list[DummyAvatar] = []
        self._send_task: asyncio.Task | None = None

    @property
    def count(self) -> int:
        return len(self.avatars)

    def start(self) -> None:
        if self._send_task is None or self._send_task.done():
            self._send_task = asyncio.create_task(self._send_loop())

    async def stop(self) -> None:
        self.despawn_all()
        if self._send_task is not None:
            self._send_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._send_task
            self._send_task = None

    def spawn(self, count: int, center_x: float, center_z: float, radius: float) -> None:
        count = max(0, int(count))
        if count <= 0:
            return

        self.start()
        radius = max(0.0, float(radius))
        for index in range(count):
            if count == 1:
                x = center_x
                z = center_z
            else:
                angle = (math.tau * index) / count
                x = center_x + math.cos(angle) * radius
                z = center_z + math.sin(angle) * radius
            self.avatars.append(DummyAvatar(self.bridge, x, z))

    def despawn_all(self) -> None:
        for avatar in self.avatars:
            avatar.close()
        self.avatars.clear()

    def move_all_to(self, tx: float, tz: float) -> None:
        for avatar in self.avatars:
            avatar.set_target(tx, tz)

    def add_waypoint(self, tx: float, tz: float) -> None:
        for avatar in self.avatars:
            avatar.add_target(tx, tz)

    def move_one_to(self, index: int, tx: float, tz: float) -> None:
        if 0 <= index < len(self.avatars):
            self.avatars[index].set_target(tx, tz)

    def add_waypoint_one(self, index: int, tx: float, tz: float) -> None:
        if 0 <= index < len(self.avatars):
            self.avatars[index].add_target(tx, tz)

    def despawn_one(self, index: int) -> bool:
        if index < 0 or index >= len(self.avatars):
            return False
        avatar = self.avatars.pop(index)
        avatar.close()
        return True

    def _build_dummy_list(self) -> list[dict]:
        return [
            {
                "index": i,
                "deviceId": avatar.device_id,
                "x": avatar.phys_x + avatar.loco_x,
                "z": avatar.phys_z + avatar.loco_z,
                "hasTarget": len(avatar.targets) > 0,
            }
            for i, avatar in enumerate(self.avatars)
        ]

    async def _send_loop(self) -> None:
        last_tick = time.monotonic()
        room_bytes = self.bridge.room_id.encode("utf-8")
        while True:
            await asyncio.sleep(DUMMY_SEND_INTERVAL_SEC)
            now = time.monotonic()
            dt = max(0.001, now - last_tick)
            last_tick = now
            if not self.avatars:
                continue

            for avatar in list(self.avatars):
                avatar.update(dt)
                transform = avatar.get_transform(now)
                payload = serialize_avatar_transform(
                    device_id=transform["device_id"],
                    head_pos=transform["head_pos"],
                    head_yaw_rad=transform["head_yaw_rad"],
                    right_rel=transform["right_rel"],
                    left_rel=transform["left_rel"],
                    physical_pos=transform["physical_pos"],
                    virtuals=transform["virtuals"],
                    pose_seq=transform["pose_seq"],
                )
                try:
                    await avatar.socket.send_multipart([room_bytes, payload])
                except Exception as exc:
                    print(f"[DUMMY] Send error for {avatar.device_id}: {exc}")

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
        self.dummy_manager = DummyAvatarManager(self)

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
            "roomSummary": self._build_room_summary(),
            "dummyCount": self.dummy_manager.count,
            "dummies": self.dummy_manager._build_dummy_list(),
        })

    def _build_bridge_status(self) -> str:
        return json.dumps({
            "type": "bridge_status",
            "roomSummary": self._build_room_summary(),
            "dummyCount": self.dummy_manager.count,
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

    async def _broadcast_dummy_status(self):
        if not self.ws_clients:
            return

        json_str = json.dumps({"type": "dummy_status", "count": self.dummy_manager.count})
        disconnected = []
        for ws in list(self.ws_clients.keys()):
            try:
                await ws.send(json_str)
            except websockets.ConnectionClosed:
                disconnected.append(ws)
        for ws in disconnected:
            del self.ws_clients[ws]

    async def _broadcast_dummy_list(self):
        if not self.ws_clients:
            return

        json_str = json.dumps({
            "type": "dummy_list",
            "dummies": self.dummy_manager._build_dummy_list(),
            "count": self.dummy_manager.count,
        })
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

                elif action == "spawn_dummies":
                    self.dummy_manager.spawn(
                        int(msg.get("count", 0)),
                        float(msg.get("centerX", 0.0)),
                        float(msg.get("centerZ", 0.0)),
                        float(msg.get("radius", 0.0)),
                    )
                    await self._broadcast_dummy_status()
                    await self._broadcast_dummy_list()

                elif action == "despawn_dummies":
                    self.dummy_manager.despawn_all()
                    await self._broadcast_dummy_status()
                    await self._broadcast_dummy_list()

                elif action == "move_dummies_to":
                    self.dummy_manager.move_all_to(float(msg.get("x", 0.0)), float(msg.get("z", 0.0)))

                elif action == "add_waypoint":
                    self.dummy_manager.add_waypoint(float(msg.get("x", 0.0)), float(msg.get("z", 0.0)))

                elif action == "move_one_to":
                    self.dummy_manager.move_one_to(
                        int(msg.get("index", -1)),
                        float(msg.get("x", 0.0)),
                        float(msg.get("z", 0.0)),
                    )

                elif action == "add_waypoint_one":
                    self.dummy_manager.add_waypoint_one(
                        int(msg.get("index", -1)),
                        float(msg.get("x", 0.0)),
                        float(msg.get("z", 0.0)),
                    )

                elif action == "despawn_one":
                    self.dummy_manager.despawn_one(int(msg.get("index", -1)))
                    await self._broadcast_dummy_status()
                    await self._broadcast_dummy_list()

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
        self.dummy_manager.start()
        asyncio.create_task(self.zmq_subscriber())
        try:
            async with websockets.serve(self.handle_ws_client, ws_host, ws_port):
                await asyncio.Future()
        finally:
            await self.dummy_manager.stop()
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
