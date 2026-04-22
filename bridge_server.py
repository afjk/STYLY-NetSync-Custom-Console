#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pyzmq",
#   "websockets",
#   "styly-netsync-server>=0.10.3",
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

try:
    from styly_netsync.adapters import client_transform_to_wire, create_stealth_transform
    from styly_netsync.binary_serializer import (
        MSG_CLIENT_VAR_SYNC,
        MSG_DEVICE_ID_MAPPING,
        MSG_GLOBAL_VAR_SYNC,
        MSG_ROOM_POSE,
        MSG_RPC,
        deserialize,
        serialize_client_transform,
        serialize_client_var_set,
        serialize_global_var_set,
        serialize_rpc_message,
    )
    from styly_netsync.types import client_transform_data, transform_data
except ModuleNotFoundError:
    client_transform_to_wire = None
    create_stealth_transform = None
    deserialize = None
    serialize_client_transform = None
    serialize_client_var_set = None
    serialize_global_var_set = None
    serialize_rpc_message = None
    client_transform_data = None
    transform_data = None
    MISSING_RUNTIME_DEPENDENCIES.append("styly-netsync-server")

POSE_FLAG_PHYSICAL_VALID = 1 << 1
POSE_FLAG_HEAD_VALID = 1 << 2
POSE_FLAG_RIGHT_VALID = 1 << 3
POSE_FLAG_LEFT_VALID = 1 << 4
POSE_FLAG_VIRTUALS_VALID = 1 << 5
DISCOVERY_REQUEST = "STYLY-NETSYNC-DISCOVER"
DISCOVERY_RESPONSE_PREFIX = "STYLY-NETSYNC"
DEFAULT_SERVER_DISCOVERY_PORT = 9999
DEFAULT_HTTP_PORT = 8080
WEB_CLIENT_FILENAME = "NetSyncWebClient.html"
DUMMY_SEND_INTERVAL_SEC = 0.1


def ensure_runtime_dependencies():
    if not MISSING_RUNTIME_DEPENDENCIES:
        return

    missing = ", ".join(MISSING_RUNTIME_DEPENDENCIES)
    print(
        "[Bridge] Missing Python packages: "
        f"{missing}. Install with `python3 -m pip install {' '.join(MISSING_RUNTIME_DEPENDENCIES)}`"
    )
    raise SystemExit(1)

def _adapt_position(raw: dict | None) -> dict[str, float]:
    raw = raw or {}
    return {
        "x": float(raw.get("posX", 0.0)),
        "y": float(raw.get("posY", 0.0)),
        "z": float(raw.get("posZ", 0.0)),
    }


def _adapt_relative_transform(head: dict | None, raw: dict | None) -> dict | None:
    if not head or not raw:
        return None

    return {
        "relPos": {
            "x": float(raw.get("posX", 0.0)) - float(head.get("posX", 0.0)),
            "y": float(raw.get("posY", 0.0)) - float(head.get("posY", 0.0)),
            "z": float(raw.get("posZ", 0.0)) - float(head.get("posZ", 0.0)),
        },
        "rotPacked": 0,
    }


def _adapt_room_pose(raw: dict) -> dict:
    clients = []
    for client in raw.get("clients", []):
        adapted = {
            "clientNo": client.get("clientNo"),
            "poseTime": client.get("poseTime"),
            "poseSeq": client.get("poseSeq"),
            "flags": client.get("flags", 0),
        }
        head = client.get("head")
        if head:
            adapted["head"] = {
                "pos": _adapt_position(head),
                "rotPacked": 0,
            }

        physical = client.get("physical")
        if physical:
            adapted["physical"] = {"pos": _adapt_position(physical)}

        right = _adapt_relative_transform(head, client.get("rightHand"))
        if right:
            adapted["right"] = right

        left = _adapt_relative_transform(head, client.get("leftHand"))
        if left:
            adapted["left"] = left

        virtuals = client.get("virtuals") or []
        if head and virtuals:
            adapted["virtuals"] = [
                _adapt_relative_transform(head, virtual)
                for virtual in virtuals
                if virtual is not None
            ]

        xr_origin_delta = {}
        for src, dst in (
            ("xrOriginDeltaX", "x"),
            ("xrOriginDeltaZ", "z"),
            ("xrOriginDeltaYaw", "yaw"),
        ):
            if src in client:
                xr_origin_delta[dst] = client.get(src, 0.0)
        if xr_origin_delta:
            adapted["xrOriginDelta"] = xr_origin_delta

        clients.append(adapted)

    return {
        "type": "room_pose",
        "roomId": raw.get("roomId", ""),
        "broadcastTime": raw.get("broadcastTime", 0.0),
        "clients": clients,
    }


def _adapt_rpc(raw: dict) -> dict:
    arguments_json = raw.get("argumentsJson", "[]")
    try:
        args = json.loads(arguments_json) if arguments_json else []
    except (TypeError, json.JSONDecodeError):
        args = []
    return {
        "type": "rpc",
        "senderClientNo": raw.get("senderClientNo", 0),
        "targetClientNos": raw.get("targetClientNos", []),
        "functionName": raw.get("functionName", ""),
        "args": args,
    }


def _adapt_id_mapping(raw: dict) -> dict:
    return {
        "type": "id_mapping",
        "serverVersion": raw.get("serverVersion", "0.0.0"),
        "mappings": [
            {
                "clientNo": mapping.get("clientNo"),
                "deviceId": mapping.get("deviceId", ""),
                "stealth": bool(mapping.get("isStealthMode", False)),
            }
            for mapping in raw.get("mappings", [])
        ],
    }


def _adapt_global_var_sync(raw: dict) -> dict:
    return {
        "type": "global_var_sync",
        "variables": [
            {
                "name": variable.get("name", ""),
                "value": variable.get("value", ""),
                "timestamp": variable.get("timestamp", 0.0),
                "lastWriter": variable.get("lastWriterClientNo", 0),
            }
            for variable in raw.get("variables", [])
        ],
    }


def _adapt_client_var_sync(raw: dict) -> dict:
    return {
        "type": "client_var_sync",
        "clientVariables": {
            client_no: [
                {
                    "name": variable.get("name", ""),
                    "value": variable.get("value", ""),
                    "timestamp": variable.get("timestamp", 0.0),
                    "lastWriter": variable.get("lastWriterClientNo", 0),
                }
                for variable in variables
            ]
            for client_no, variables in (raw.get("clientVariables") or {}).items()
        },
    }


def deserialize_sub_message(topic: bytes, payload: bytes) -> dict | None:
    del topic
    if not payload:
        return None
    try:
        msg_type, data, _ = deserialize(payload)
    except Exception as exc:
        print(f"Deserialize error: {exc}")
        return None

    if data is None:
        return None

    if msg_type == MSG_ROOM_POSE:
        return _adapt_room_pose(data)
    if msg_type == MSG_RPC:
        return _adapt_rpc(data)
    if msg_type == MSG_DEVICE_ID_MAPPING:
        return _adapt_id_mapping(data)
    if msg_type == MSG_GLOBAL_VAR_SYNC:
        return _adapt_global_var_sync(data)
    if msg_type == MSG_CLIENT_VAR_SYNC:
        return _adapt_client_var_sync(data)
    return None


def make_stealth_handshake(device_id: str) -> bytes:
    tx = create_stealth_transform()
    tx.device_id = device_id
    return serialize_client_transform(client_transform_to_wire(tx))


def make_rpc(
    function_name: str,
    args: list[str],
    sender_client_no: int = 0,
    target_client_nos: list[int] | None = None,
) -> bytes:
    return serialize_rpc_message(
        {
            "senderClientNo": sender_client_no,
            "targetClientNos": target_client_nos or [],
            "functionName": function_name,
            "argumentsJson": json.dumps(args),
        }
    )


def make_global_var_set(sender_client_no: int, name: str, value: str) -> bytes:
    return serialize_global_var_set(
        {
            "senderClientNo": sender_client_no,
            "variableName": name,
            "variableValue": value,
            "timestamp": time.time(),
        }
    )


def make_client_var_set(
    sender_client_no: int,
    target_client_no: int,
    name: str,
    value: str,
) -> bytes:
    return serialize_client_var_set(
        {
            "senderClientNo": sender_client_no,
            "targetClientNo": target_client_no,
            "variableName": name,
            "variableValue": value,
            "timestamp": time.time(),
        }
    )


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

    def build_payload(self, now: float) -> bytes:
        moving = bool(self.targets)
        amplitude = 0.2 if moving else 0.03
        frequency = 4.0 if moving else 0.8
        swing = amplitude * math.sin((math.tau * frequency * now) + self.hand_phase)
        lift = abs(swing) * 0.15

        head_x = self.phys_x + self.loco_x
        head_y = self.head_height
        head_z = self.phys_z + self.loco_z
        half_yaw = self.head_yaw_rad * 0.5
        head_quat = (0.0, math.sin(half_yaw), 0.0, math.cos(half_yaw))

        right_x = head_x + 0.24
        right_y = head_y - 0.34 + lift
        right_z = head_z + 0.18 + swing
        left_x = head_x - 0.24
        left_y = head_y - 0.34 + lift
        left_z = head_z + 0.18 - swing

        virtuals = []
        for orbit in self.virtual_orbits:
            angle = (now * orbit["speed"]) + orbit["phase"]
            virtuals.append(
                transform_data(
                    pos_x=head_x + math.cos(angle) * orbit["radius"],
                    pos_y=head_y + orbit["height"] + (0.05 * math.sin(angle * 1.7)),
                    pos_z=head_z + math.sin(angle) * orbit["radius"],
                )
            )

        flags = POSE_FLAG_PHYSICAL_VALID | POSE_FLAG_HEAD_VALID | POSE_FLAG_RIGHT_VALID | POSE_FLAG_LEFT_VALID
        if virtuals:
            flags |= POSE_FLAG_VIRTUALS_VALID

        transform = client_transform_data(
            device_id=self.device_id,
            pose_seq=self.pose_seq,
            flags=flags,
            head=transform_data(
                pos_x=head_x,
                pos_y=head_y,
                pos_z=head_z,
                rot_x=head_quat[0],
                rot_y=head_quat[1],
                rot_z=head_quat[2],
                rot_w=head_quat[3],
            ),
            right_hand=transform_data(pos_x=right_x, pos_y=right_y, pos_z=right_z),
            left_hand=transform_data(pos_x=left_x, pos_y=left_y, pos_z=left_z),
            physical=transform_data(
                pos_x=self.phys_x,
                pos_y=0.0,
                pos_z=self.phys_z,
                rot_x=head_quat[0],
                rot_y=head_quat[1],
                rot_z=head_quat[2],
                rot_w=head_quat[3],
                is_local_space=True,
            ),
            virtuals=virtuals,
        )
        wire = client_transform_to_wire(transform)
        wire["xrOriginDeltaX"] = head_x - self.phys_x
        wire["xrOriginDeltaZ"] = head_z - self.phys_z
        wire["xrOriginDeltaYaw"] = 0.0

        self.pose_seq = (self.pose_seq + 1) & 0xFFFF
        return serialize_client_transform(wire)


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
                payload = avatar.build_payload(now)
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

        handshake = make_stealth_handshake(device_id)
        await dealer.send_multipart([self.room_id.encode("utf-8"), handshake])
        print(f"[DEALER] Sent stealth handshake for {device_id}")

        # ★ 接続時に現在のNVスナップショットを送信
        await ws.send(self._build_snapshot())

        async def keepalive():
            while ws in self.ws_clients:
                await asyncio.sleep(2.0)
                try:
                    hs = make_stealth_handshake(device_id)
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
                    payload = make_rpc(
                        msg["functionName"],
                        msg.get("args", []),
                        msg.get("senderClientNo", 0),
                        msg.get("targetClientNos")
                    )
                    await dealer.send_multipart([room, payload])

                elif action == "set_global_var":
                    payload = make_global_var_set(
                        msg.get("senderClientNo", 0),
                        msg["name"], msg["value"]
                    )
                    await dealer.send_multipart([room, payload])

                elif action == "set_client_var":
                    payload = make_client_var_set(
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
