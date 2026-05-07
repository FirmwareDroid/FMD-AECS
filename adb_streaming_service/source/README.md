# ADB Streaming Service (adb_streaming_service)

This subproject implements a WebSocket-based ADB streaming service that can connect to remote or local Android devices (emulators or physical devices) and provide real-time control and media streaming to web clients.

The service includes:
- WebSocket transport for video, audio and control messages (based on scrcpy / @yume-chan client abstractions)
- HTTP API for management endpoints (metainfo, install, start, pin/unpin)
- Basic authentication middleware (optional) for HTTP APIs
- WebSocket upgrade authentication (supports Authorization header, `auth` query param, and origin userinfo)
- SSL/TLS (HTTPS/WSS) support via environment variables
- Volume control via key-injection (no subprocess.exec)
- Robust audio handling and diagnostic logging for troubleshooting

---

Table of contents
- [Getting started](#getting-started)
- [Environment variables](#environment-variables)
- [Running](#running)
- [HTTP API](#http-api)
- [WebSocket API](#websocket-api)
- [Volume control](#volume-control)
- [Authentication & security](#authentication--security)
- [Troubleshooting](#troubleshooting)
- [Development notes](#development-notes)


## Getting started

Prerequisites
- Node.js (v20 or v22 recommended)
- pnpm (used for installing dependencies)
- adb (Android platform tools) available in PATH
- Docker (optional, for integration with emulator images)

Install dependencies (in `adb_streaming_service/source`):

```bash
pnpm install
```

## Environment variables

You can place environment variables in the repository `.env` files. Key variables for this service:

- `HOST` — host address to bind (default: `0.0.0.0`)
- `PORT` — HTTP/WebSocket port (default: `9001`)
- `SSL` — set to `true` or `1` to enable HTTPS/WSS
- `SSL_KEY_PATH` — path to private key file (required if `SSL` enabled)
- `SSL_CERT_PATH` — path to certificate file (required if `SSL` enabled)
- `AUTH_ENABLED` — set to `true` or `1` to enable Basic auth for HTTP endpoints
- `AUTH_USER` — Basic auth username
- `AUTH_PASS` — Basic auth password

### ADB server discovery

The service can connect to ADB servers in two complementary ways:

**Auto-discovery (default, Docker-friendly)**

On startup the service scans every host address on the container's local private IPv4 subnet (clamped to /24 = 254 hosts) for an open TCP connection on port 5037.  Only [RFC 1918](https://datatracker.ietf.org/doc/html/rfc1918) private addresses are ever probed.  Newly started ADB containers are picked up periodically without a restart.

| Variable | Default | Description |
|---|---|---|
| `ADB_DISCOVERY_ENABLED` | `true` | Set to `false` to disable auto-discovery entirely. |
| `ADB_DISCOVERY_PORT` | `5037` | ADB port to probe on each candidate host. |
| `ADB_DISCOVERY_TIMEOUT_MS` | `500` | Per-host TCP probe timeout in milliseconds. |
| `ADB_DISCOVERY_REFRESH_INTERVAL_MS` | `60000` | How often (ms) to re-scan for new containers. Set to `0` to disable. |
| `ADB_DISCOVERY_SUBNETS` | _(none)_ | Optional extra subnets to scan (`"addr/mask"`, comma-separated). |

**Static list (optional)**

Set `ADB_SERVER_LIST` to a comma-separated list of `host:port` endpoints to add known servers directly. When not set the service relies entirely on auto-discovery.  When both are set, the lists are merged and deduplicated.

- `ADB_SERVER_LIST` — e.g. `192.168.1.10:5037,192.168.1.11:5037` (optional)

> **Fallback:** If neither static config nor discovery produces any reachable server, `localhost:5037` is tried as a last resort (useful for single-container / development setups).

The startup script attempts to load `.env` from several fallback locations. The primary file is `adb_streaming_service/source/.env` then `env/adb_streamer/.env`, etc. You can also set environment variables directly in your environment or Docker container.

## Running

Development (run locally):

```bash
# in adb_streaming_service/source
pnpm run build-start
# or directly
cd /app/source
npx @yume-chan/fetch-scrcpy-server 3.3.3 && node index.js
```

Docker (multi-stage build included): see top-level `Dockerfile` in project root and `create_docker_startup_scripts.py` for image generation.

## HTTP API

Routes (examples):

- `GET /api/health/get` — health check (public, does not require auth)
- `GET /api/meta/get-all` — returns meta information about the service (requires auth if `AUTH_ENABLED=true`)
- `POST /api/adb/install` — install APK on device (requires auth if enabled)
- `POST /api/adb/start` — start a device stream (requires auth if enabled)
- `POST /api/adb/pin` — pin a device to a client session (requires auth if enabled)
- `POST /api/adb/unpin` — unpin device (requires auth if enabled)

All HTTP endpoints (except `/api/health/*`) are protected by default when `AUTH_ENABLED=true`.

### Authentication for HTTP

- When `AUTH_ENABLED=true`, the server expects Basic auth credentials either via the `Authorization` header or via the `auth` query parameter (which can be a base64 payload or `Basic <payload>`).
- Example with `curl`:

```bash
curl -u $AUTH_USER:$AUTH_PASS http://localhost:9001/api/meta/get-all
```

## WebSocket API

The streaming/control channel is provided via WebSocket. Connect to `ws://host:port` or `wss://host:port` when SSL is enabled.

The WebSocket upgrade requires authentication too — it uses the same Basic scheme but allows a few transport conveniences:

- `Authorization` header with `Basic` token
- `auth` query parameter (base64 token or `Basic <payload>`)
- `origin` header that includes userinfo `wss://user:pass@host` (legacy)

Client upgrade example (wss):

```
wss://your-server:9001/?id=your-session-id&device=emulator-5554&video=true&audio=true

# You can include auth as query param (base64 user:pass)
wss://your-server:9001/?id=...&device=...&auth=<base64>
```

After upgrade the server will send/receive message-packed (msgpack) frames. The service uses `msgpackr` for packing/unpacking frames. Messages have the shape:

```
{ cmd?: string, payload?: any }
```

Common `cmd` values (examples):
- `injectTouch` — inject touch event
- `injectKeyCode` — inject key event
- `injectScroll` — inject scroll event
- `setDeviceVolume` — set/increase/decrease/mute volume (see next section)

The server accepts and gracefully normalizes a variety of payload shapes for touch/scroll events (client coordinates, normalized coords, device coords). The service performs a best-effort normalization and will either send an error message back through WS or attempt a fallback normalization.

## Volume control

`setDeviceVolume` (aliased to `setVolume`) is designed to be safe and avoid calling `subprocess.exec` on the host. Instead, it uses the scrcpy controller to inject Android key events:

- VolumeUp `keyCode=24`
- VolumeDown `keyCode=25`
- Mute `keyCode=164`

Payload shapes accepted:

- `{ volume: 0 }` or `{ volume: 1 }` — treated as a single small step decrease/increase (to avoid sudden full mute/full volume)
- `{ volume: 0.3 }` or `0.3` — treated as best-effort absolute target (the server will clamp to an assumed volume steps and issue multiple presses)
- `{ delta: -3 }` — press VolumeDown three times
- `{ action: 'up' }` or `'up'` — single VolumeUp press
- `{ muted: true }` — mute via Mute key

The server responds with a message-packed message `media: 'message', type: 'volume_set'` containing `payload: {ok: true|false, ...}` describing the result.

## Authentication & security

- HTTP authentication (Basic) is enforced by middleware for all non-health routes when `AUTH_ENABLED=true`.
- WebSocket upgrade is protected by `requireAuthForUpgrade` and supports the three transport methods listed above.
- To enable TLS set `SSL=true` and provide `SSL_KEY_PATH` and `SSL_CERT_PATH` (server will abort startup if missing).

## Troubleshooting

1. WebSocket upgrade fails with 401:
   - Ensure `AUTH_ENABLED`, `AUTH_USER`, `AUTH_PASS` are set and your client provides valid credentials.
   - Use `?auth=<base64>` query parameter as a convenience.

2. Audio not playing or lots of dropped audio:
   - The server performs validation of audio packets (expects codec-specific formats). Check logs for `Audio packet with unexpected shape/type` and `audio_invalid` messages.
   - Ensure clients signal `audio_ready` before expecting continuous audio streaming.

3. Volume changes not applied:
   - Volume is applied using key injection. Ensure the scrcpy controller is active for the websocket session (server logs errors if the controller is not yet available).

4. SSL startup aborts:
   - If `SSL=true` the server requires `SSL_KEY_PATH` and `SSL_CERT_PATH` to exist and be readable by the process.

5. `uWebSockets.js` native binding errors on ARM laptops/containers:
   - Ensure your container's glibc version matches requirements for the `uWebSockets.js` compiled binary or rebuild in a compatible environment. For containerized builds use a x86_64 builder or pick a Node base image matching the platform.

## Development notes

- The server is implemented in `index.js` and uses `App` and `Route` wrappers in `utils/http/` to unify HTTP and WS handling.
- The `Route` object accepts `method`, `url`, `handler`, and optional `upgrade/open/message` fields. Authentication middleware is installed via `app.use(httpAuthMiddleware)`.
- Audio handling includes packet unwrapping helpers and validation (`validateAudioPacket`). When audio packets are invalid the server sends `audio_invalid` messages and tracks drop statistics.

## Contribution

If you'd like to contribute, please create a PR with changes and tests where appropriate. For client-side protocol changes, ensure backward compatibility or provide a migration path.

---

(End)
