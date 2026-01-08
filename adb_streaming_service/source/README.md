# ADB Streaming Service

A small, production-ready Node.js service that proxies Android device video/audio/control streams over WebSockets using ADB + scrcpy tooling. It exposes both HTTP endpoints for simple status/debug information and a WebSocket endpoint that speaks msgpack-packed control and media messages. The service is designed to run behind TLS in production and supports basic HTTP authentication.

This README explains how to configure, run, and integrate with the service and contains examples to help you test basic functionality.

---

## Table of contents

- [What it does](#what-it-does)
- [Prerequisites](#prerequisites)
- [Configuration (.env)](#configuration-env)
- [Run (development)](#run-development)
- [Run (production / pm2)](#run-production--pm2)
- [HTTP & WebSocket API](#http--websocket-api)
  - [Status endpoint](#status-endpoint)
  - [WebSocket upgrade & query params](#websocket-upgrade--query-params)
  - [Message format (msgpack)](#message-format-msgpack)
  - [Common commands](#common-commands)
- [Examples](#examples)
  - [Node test: send setVolume small-step](#node-test-send-setvolume-small-step)
- [Debugging & logs](#debugging--logs)
- [Troubleshooting](#troubleshooting)
- [Notes & internals](#notes--internals)
- [Contributing](#contributing)
- [License](#license)

---

## What it does

- Starts an HTTPS (or HTTP) server and a WebSocket endpoint that upgrades clients to a streaming/control session.
- Uses an ADB-over-TCP service and `@yume-chan/scrcpy` to start streaming from connected Android devices.
- Forwards video and audio frames to connected clients via msgpack-packed WebSocket messages.
- Accepts control messages (touch, scroll, key events) from clients and injects them into the device using the scrcpy controller.
- Implements helper commands such as `setVolume` (mapped to Android keycodes), `injectTouch`, `injectScroll`, `injectKeyCode`.
- Provides debug HTTP endpoints (e.g. `/_debug/audio_stats`).

---

## Prerequisites

- Node.js (v18+ recommended) and `pnpm`/`npm` installed.
- ADB available (the environment expects an ADB server compatible with the `@yume-chan` stack). The project includes configuration to locate an ADB version via env.
- `@yume-chan` dependencies (installed via `pnpm install` or `npm install`).
- If SSL is enabled you must have the certificate and key available on disk and pointed to by environment variables.

---

## Configuration (.env)

The service loads environment variables from several locations (see `source/index.js`); the main options are:

- `HOST` — host to bind to (default: `0.0.0.0`)
- `PORT` — port to listen on (default: `9001`)
- `SSL` — `true` or `false`. If `true`, the server expects SSL key/cert paths and will serve HTTPS/WSS.
- `SSL_KEY_PATH` — path to the SSL private key file (required when `SSL=true`).
- `SSL_CERT_PATH` — path to the SSL certificate file (required when `SSL=true`).
- `AUTH_ENABLED` — `true` or `false`. When `true` HTTP/WebSocket upgrade require Basic auth.
- `AUTH_USER` and `AUTH_PASS` — username/password for Basic HTTP auth when `AUTH_ENABLED=true`.

Example `.env` (the repository contains a sample under `env/adb_streamer/.env`):

```
HOST=0.0.0.0
PORT=9001
SSL=true
SSL_KEY_PATH=/path/to/priv.key
SSL_CERT_PATH=/path/to/cert.pem
AUTH_ENABLED=false
```

The service also accepts `ENV_FILE` or `DOTENV_PATH` environment variables to point to a specific env file.

---

## Run (development)

Install dependencies and run in development mode:

```bash
# from repository root (adb_streaming_service/source)
pnpm install
pnpm run dev
```

This launches the server with `nodemon` (see `package.json` scripts). If you need to fetch a compatible scrcpy server binary you can run the helper from `@yume-chan/fetch-scrcpy-server` (see `package.json:build-start`).

---

## Run (production / pm2)

The project contains a `pm2` configuration. To run under `pm2`/`pm2-runtime`:

```bash
# start with pm2 runtime (recommended in production container)
pm run start
# or with pm2 directly
pm2 start pm2.config.cjs
pm2 logs <app-name>
```

When running under pm2, ensure the `.env` file is accessible to the process (pm2 can be started with environment variables set, or you can point to an `ENV_FILE`).

---

## HTTP & WebSocket API

The service exposes an HTTP status endpoint and a WebSocket endpoint that clients use for streaming and control. WebSocket messages are msgpack-packed objects (uses `msgpackr` `Packr` / `Unpackr`).

### Status endpoint

- `GET /` — will reply with `ADB streaming service is running.` (requires auth if enabled)
- `GET /_debug/audio_stats` — returns JSON with current per-user audio statistics (requires auth if enabled)

### WebSocket upgrade & query params

Upgrade path is the same as the Web server root; to upgrade to a streaming session connect to WSS/WS with query parameters:

- `id` — session identifier (required). The server uses this to keep per-user state.
- `device` — the device serial to connect to (optional if exactly one device is connected; the server will auto-select when appropriate)
- `audio` — `true` to enable audio forwarding (default: `false`)
- `audioCodec` — codec preference (e.g. `raw`, `aac`, `opus`)
- `video` — `true` to enable video streaming (default: `true` when omitted)
- `videoCodec` — codec preference (e.g. `h264`)
- Other optional parameters: `displayId`, `maxSize`, `maxFps`, `videoBitRate`

Example WSS URL:

```
wss://yourhost:9001/?id=session-123&device=emulator-5554&audio=true&video=true
```

If `AUTH_ENABLED=true`, the WebSocket upgrade must be performed with a valid Basic Authorization header.

### Message format (msgpack)

All messages sent and received over the WebSocket are packed with msgpack using the `msgpackr` library. Each message is an object with at least a `cmd` property for client->server control messages, or `media` for server->client messages. Examples below assume a `Packr` client.

Server->Client messages examples:
- `{ media: 'video', packet }` — video frame packet
- `{ media: 'audio', packet }` — audio frame packet (binary Uint8Array)
- `{ media: 'audio_metadata', packet }` — audio metadata (codec, channels, sample rate)
- `{ media: 'message', type: 'volume_set', payload: { ok: true, action: 'small-increase' } }` — informational message

Client->Server commands (examples):
- `cmd: 'setVolume'` with payload:
  - number `0` or `1` — (small step) will perform a single volume down/up press
  - object `{ volume: 0.5 }` — best-effort set to ~50% via repeated key presses (slow)
  - object `{ delta: -3 }` — press volume down 3 steps
  - object `{ muted: true }` — will send the MUTE keycode
- `cmd: 'injectTouch'` with payload: an object or an array of objects representing touch events (the server normalizes many shapes). The normalized shape the scrcpy controller expects contains fields such as `action`, `pointerId` (BigInt), `pointerX`, `pointerY`, `videoWidth`, `videoHeight`, `pressure`, `buttons`.
- `cmd: 'injectScroll'` — normalized shape includes `pointerX`, `pointerY`, `scrollX`, `scrollY`, `pointerId`.
- `cmd: 'injectKeyCode'` — inject a key event specified by `{ action: 0|1, keyCode: <AndroidKeyCode> }`.

The server provides robust normalization for `injectTouch` and `injectScroll` payloads; if normalization fails the server logs a helpful message and rejects the action.

### Message packing/unpacking

Clients must use msgpack (Packr/Unpackr) to serialize/deserialize messages. Example Node code to pack a command:

```javascript
import { Packr, Unpackr } from 'msgpackr';
const packr = new Packr();
const data = packr.pack({ cmd: 'setVolume', payload: 0 });
ws.send(data);
```

---

## Examples

### Node test: send setVolume small-step

A quick Node script that connects to the service and sends small-step volume commands (0 = small decrease, 1 = small increase). Save as `test-set-volume.js` and run `node test-set-volume.js`.

```javascript
import WebSocket from 'ws';
import { Packr, Unpackr } from 'msgpackr';
const packr = new Packr();
const unpackr = new Unpackr();

const ws = new WebSocket('wss://localhost:9001/?id=test&device=emulator-5554', { rejectUnauthorized: false });
ws.on('open', () => {
  ws.send(packr.pack({ cmd: 'setVolume', payload: 0 })); // small decrease
  setTimeout(() => ws.send(packr.pack({ cmd: 'setVolume', payload: 1 })), 1000); // small increase
});
ws.on('message', (data) => {
  const o = unpackr.unpack(data);
  console.log('recv', o);
});

```

---

## Debugging & logs

- Logs are implemented via `pino`; the logger is initialized from `source/services/logger.js`. The service prints startup information (loaded env file, SSL status, listening address).
- Useful debug endpoints:
  - `GET /` — quick status check
  - `GET /_debug/audio_stats` — inspect per-session audio statistics (packets sent/dropped)
- The server logs incoming WS messages as concise descriptors (cmd + payload summary) and extensive debug details when normalization or serialization issues occur.

If you run into `ERR_CONNECTION_REFUSED`, ensure the service is started (the CLI startup now calls `run()` in `source/index.js`) and check for TLS errors after the server is up.

---

## Troubleshooting

- Certificate/TLS errors: if you use self-signed certs your browser will reject the connection. Test with `curl -k https://localhost:9001/` to confirm the server is responding.
- Controller unavailable: many control commands (e.g., `injectKeyCode`) require the scrcpy controller to be connected. The server will respond with an error message when the controller is not available on the connection.
- Audio frames dropped / unexpected shape: the server validates audio frames and logs detailed previews when it rejects frames. Ensure the scrcpy audio encoder emits raw Uint8Array or another supported format.
- BigInt serialization errors: The server sanitizes payloads to keep `pointerId` as BigInt while converting other BigInt fields to numbers. If you see `Cannot mix BigInt and other types` exceptions, the client may be sending a mixture that the scrcpy writer cannot serialize — share the exact payload and server logs and the helpers will be adjusted.

---

## Notes & internals

- The server uses `msgpackr` to pack/unpack messages (Packr/Unpackr) for compact binary transport.
- `@yume-chan` libraries are used to manage ADB and scrcpy integration; check `package.json` for versions.
- The service performs multiple normalization layers for input commands (`normalizeTouchPayload`, `fallbackNormalizePayload`, `normalizeScrollPayload`) to be resilient to different client payload shapes.
- `setVolume` uses injected Android keycodes (VolumeUp/VolumeDown/Mute) rather than calling `adb shell` repeatedly, which is more reliable in many setups. The code distinguishes small-step (exact `0` / `1`) vs. bulk/best-effort changes via repeated presses.

---

## Contributing

- Fork the repository, make changes, and open a PR. Please follow the project's code style and run a local smoke test (start service + test WebSocket messaaging) before submitting.

---

## License

MIT

---


