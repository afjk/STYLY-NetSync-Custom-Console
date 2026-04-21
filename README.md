# STYLY NetSync Custom Console

Browser-based management console for STYLY NetSync Server.

This project uses a local Python bridge because the STYLY NetSync REST API is mainly for setting Network Variables and does not expose the full real-time room state. The bridge joins the NetSync room over ZeroMQ, caches live messages, and forwards them to the browser over WebSocket.

GitHub Pages: https://afjk.github.io/STYLY-NetSync-Custom-Console/

## Files

- `index.html` - GitHub Pages entry point
- `NetSyncWebClient.html` - browser console UI
- `bridge_server.py` - WebSocket bridge, NetSync discovery, and static web console server
- `start_bridge_server.sh` - launcher script
- `idea/` - design notes and future plans

## Requirements

- Python 3
- `uv` recommended

When `uv` is available, `start_bridge_server.sh` runs `bridge_server.py` as a uv script and installs the required Python dependencies automatically:

- `pyzmq`
- `websockets`

Without `uv`, install dependencies manually:

```bash
python3 -m pip install pyzmq websockets
```

## Start

```bash
./start_bridge_server.sh
```

By default, the bridge:

- discovers STYLY NetSync Server through the normal discovery flow
- serves the web console on `http://<bridge-ip>:8080/`
- accepts browser WebSocket connections on `ws://<bridge-ip>:8765`
- subscribes to room `default_room`

Typical startup output:

```text
[Bridge] Discovering NetSync server on port 9999...
[Bridge] Discovered NetSync server 'STYLY-NetSync-Server' at tcp://192.168.1.20 (dealer:5555, sub:5556, via udp-broadcast)
[HTTP] Web console URLs:
  - http://127.0.0.1:8080/
  - http://192.168.1.10:8080/
[Bridge] Reachable URLs:
  - ws://127.0.0.1:8765
  - ws://192.168.1.10:8765
```

Open the HTTP URL from the same Mac or another PC on the network.

## External PC Access

Run the bridge on the Mac:

```bash
./start_bridge_server.sh
```

Then open this URL from another PC:

```text
http://<mac-ip>:8080/
```

The web console automatically uses:

```text
ws://<mac-ip>:8765
```

Make sure the Mac firewall allows incoming TCP connections for ports `8080` and `8765`.

## Manual NetSync Server Address

If discovery is not available across the current network, specify the NetSync server explicitly:

```bash
./start_bridge_server.sh --server tcp://192.168.1.20
```

Custom room:

```bash
./start_bridge_server.sh --room my_room
```

Bind the console and WebSocket server to one network interface:

```bash
./start_bridge_server.sh --http-host 192.168.1.10 --ws-host 192.168.1.10
```

Disable HTTP serving if you only want the WebSocket bridge:

```bash
./start_bridge_server.sh --no-http
```

## Console Features

- live participant grid
- double-click participant detail panel
- deviceId / clientNo mapping display
- pose activity display
- global Network Variable display and set
- client Network Variable display and set
- RPC send from selected client detail
- simple top-down map view

## Network Notes

STYLY NetSync discovery generally works on the same subnet. Across different subnets, UDP broadcast discovery may not reach the server. In that case, use `--server tcp://<server-ip>`.

For one Mac with multiple network interfaces, the bridge binds to `0.0.0.0` by default, so the web console and WebSocket bridge are reachable through any active interface IP. Use `--http-host` and `--ws-host` when you need to restrict access to a specific interface.
