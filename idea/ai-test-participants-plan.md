# AI Test Participants Plan

## Goal

Allow AI-controlled test participants to join STYLY NetSync as normal clients for multi-user testing.

The first target is a practical simulator for load and behavior testing:

- Spawn many normal NetSync clients.
- Show them in the existing participant grid and map.
- Move them to map-selected destinations at realistic speeds.
- Support a registered map image with a configurable scale.

This is an idea document only. No implementation is assumed yet.

## Background

The existing REST API is mostly for setting Network Variables. It does not provide full real-time read access for:

- current global variables
- current client variables
- deviceId / clientNo mappings
- room pose updates
- RPC and Network Variable sync streams

The current WebSocket bridge exists because it joins the NetSync room through ZeroMQ and subscribes to real-time server messages. AI participants should fit into that architecture.

## Proposed Architecture

```text
Browser UI
  -> WebSocket bridge
  -> AI simulator manager
  -> normal NetSync clients
  -> STYLY NetSync Server
```

The simulator should not be implemented only in browser JavaScript. Pose generation and ZeroMQ traffic should run on the backend side for stability, especially for 50 to 100+ clients.

## Normal Client Behavior

Each AI participant should act as a normal NetSync client, not a stealth monitoring client.

Expected behavior:

- Each AI participant has a stable `deviceId`.
- Each sends normal `MSG_CLIENT_POSE` messages.
- The server assigns a `clientNo`.
- The participant appears in normal room mappings.
- Pose updates are broadcast like real clients.

This makes the simulator useful for testing the actual room state, mapping, Network Variables, and broadcast behavior.

## Map Support

The UI should support registering a map image and calibrating it to world coordinates.

Minimum map features:

- Upload or select a map image.
- Define pixel-to-meter scale.
- Convert image coordinates to NetSync world `x,z`.
- Click a point on the map to set an AI destination.

Possible calibration options:

- Direct scale: `1 pixel = N meters`.
- Two-point calibration: user selects two points and enters real-world distance.
- Optional world origin offset.
- Optional rotation alignment if the map image is not aligned with Unity world axes.

## Movement Model

The first movement model can be simple but physically plausible.

Suggested defaults:

- normal walk speed: `1.2 m/s`
- slow walk speed: `0.7 m/s`
- fast walk speed: `1.8 m/s`
- acceleration: `1.0 m/s^2`
- turn speed: `120 deg/s`
- pose update rate: `10 Hz`

Movement behavior:

- Move in the `x,z` plane toward the target.
- Clamp speed by configured walk speed.
- Smooth acceleration and deceleration.
- Rotate toward travel direction.
- Stop when within a small arrival radius, for example `0.15 m`.

Head and hands can initially be approximate:

- head position above body center
- hands relative to head with fixed offsets
- optional small procedural sway later

## Multi-Participant Control

Initial controls:

- spawn N AI participants
- remove all AI participants
- select one participant
- set destination by clicking the map
- set destination for all participants
- set speed profile

Useful later controls:

- random walk
- patrol points
- gather
- disperse
- follow leader
- occupancy/load test mode

## Implementation Options

### Option A: Integrate Into `bridge_server.py`

Pros:

- Simple deployment.
- Existing WebSocket UI can control everything directly.
- Existing ZeroMQ connection utilities can be reused.

Cons:

- `bridge_server.py` may become too large.
- Monitoring bridge and simulation manager responsibilities mix together.

### Option B: Separate `ai_simulator.py` Module

Pros:

- Clear responsibility boundary.
- Easier to test movement logic independently.
- Bridge can remain a UI gateway.

Cons:

- Slightly more structure required.

Preferred direction: Option B. Keep simulator logic in a separate module and expose commands through the existing WebSocket bridge.

## Scaling Considerations

For realistic server load, AI participants should use behavior close to real clients.

Open questions:

- Should each AI participant use its own DEALER socket?
- Or should one process multiplex many participants?
- What is the target maximum: 50, 100, 300, or more?

Initial recommendation:

- Use one Python process.
- Start with one DEALER socket per AI participant for realism.
- Revisit multiplexing only if scaling or resource use becomes a problem.

## Risks

- Browser rendering can become expensive with many participants; backend simulation avoids most of this.
- Map calibration needs a clear coordinate convention.
- If too many simulated clients use individual sockets, local resource limits may matter.
- The existing binary serializer logic should be reused or closely matched to avoid protocol drift.

## MVP

1. Add backend AI participant manager.
2. Spawn configurable number of normal AI clients.
3. Send normal pose updates at 10 Hz.
4. Add map image display in UI.
5. Add simple pixel-to-meter scale.
6. Click destination on map.
7. Move selected AI participant to destination at realistic walking speed.
8. Show AI participants in the existing participants grid and map.

## Decisions To Make Before Implementation

- Target maximum AI participant count.
- Whether map image should be stored in browser localStorage or backend files.
- Whether calibration needs rotation support in the first version.
- Whether collision avoidance is needed for MVP.
- Whether AI participants should set identifying client variables such as `name=AI-001`.
