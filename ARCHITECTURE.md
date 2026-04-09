# FMD-AECS Architecture

## Overview

FMD-AECS (FirmwareDroid Android Emulator Control & Streaming Service) is a
Node.js server that exposes Android devices over WebSocket. It uses
[scrcpy](https://github.com/Genymobile/scrcpy) as the underlying screen-capture
and control engine, accessed via the
[@yume-chan/adb-scrcpy](https://github.com/yume-chan/ya-webadb/tree/main/libraries/adb-scrcpy)
TypeScript library.

```
             ┌─────────────────────────────────────────────────────────┐
             │                   FMD-AECS Process                      │
             │                                                         │
  Browser /  │  uWebSockets.js              ADB Server                │
  Client  ◄──┤  (WebSocket + HTTP)   ──►   (adb-tcp-service)  ──►  Devices
             │                                                         │
             └─────────────────────────────────────────────────────────┘
```

## Directory Structure

```
adb_streaming_service/
├── Dockerfile
├── pm2.config.cjs          # PM2 process manager config
├── source/
│   ├── index.js            # Entry point (~400 lines): env loading, server startup
│   ├── .env.example        # All supported environment variables
│   ├── package.json        # Dependencies (pino, msgpackr, uWebSockets.js, …)
│   │
│   ├── services/
│   │   ├── logger.js                   # pino logger factory
│   │   └── adb/
│   │       ├── adb-tcp-service.js      # ADB device pool + scrcpy client lifecycle
│   │       └── adb-shell-service.js    # Low-level ADB shell helper
│   │   └── file/
│   │       └── file-service.js         # Multipart file upload handler
│   │
│   ├── streaming/
│   │   ├── audio.js        # Audio packet pipeline (wait, validate, send, backpressure)
│   │   └── video.js        # Video frame pipeline (metadata, pipe to WebSocket)
│   │
│   ├── websocket/
│   │   ├── upgrade.js      # HTTP → WebSocket upgrade (auth + query param parsing)
│   │   ├── open.js         # New session: device resolution, client start, stream setup
│   │   ├── message.js      # Incoming command routing (touch, scroll, key, volume, …)
│   │   └── close.js        # Session teardown + drain handler
│   │
│   ├── routes/
│   │   ├── index.js        # Route registry
│   │   ├── adb/            # ADB device list / info routes
│   │   ├── file/           # APK upload routes
│   │   ├── health/         # GET /api/health
│   │   ├── meta/           # GET /api/meta
│   │   └── debug/
│   │       └── audio-stats.js  # GET /_debug/audio_stats
│   │
│   ├── utils/
│   │   ├── error.js        # serializeError() — safe error → plain-object conversion
│   │   ├── timeout.js      # withTimeout() — Promise race with AbortController
│   │   ├── auth.js         # HTTP Basic auth (timing-safe) + middleware factories
│   │   ├── http/
│   │   │   ├── app.js      # uWebSockets.js App wrapper with graceful shutdown
│   │   │   ├── route.js    # Route descriptor wrapper
│   │   │   └── middie/
│   │   │       └── cors.js # CORS header middleware
│   │   └── normalizers/
│   │       ├── touch.js    # normalizeTouchPayload / fallbackNormalizePayload
│   │       ├── scroll.js   # normalizeScrollPayload
│   │       └── audio.js    # validateAudioPacket / extractAudioBuffer
│   │
│   ├── state/
│   │   └── global.js       # Module-singleton: global.users Map<id, UserSession>
│   │
│   ├── static/             # Static file assets
│   ├── apps/               # scrcpy server binaries (fetched at build time)
│   ├── uploads/            # APK upload staging directory
│   └── tests/
│       ├── audio.test.js
│       ├── auth.test.js
│       ├── error.test.js
│       ├── scroll.test.js
│       └── touch.test.js
```

## Key Data Flows

### 1. WebSocket Session Lifecycle

```
Client                   index.js / websocket/upgrade.js
  │── HTTP Upgrade ────►  Auth check (requireAuthForUpgrade)
  │                        Parse query params → ws user data
  │
  │── WS Open ──────────►  websocket/open.js
  │                          Device resolution (bare serial or host:port/serial)
  │                          adbTcpService.getDeviceAdb(deviceObj)
  │                          adbTcpService.start(deviceAdb, user)
  │                          streaming/audio.js  (setupAudioStream)
  │                          streaming/video.js  (setupVideoStream)
  │
  │◄─ video_metadata ────   video metadata sent to client
  │◄─ audio_metadata ────   audio metadata sent to client
  │◄─ video / audio ─────   continuous frame/packet stream
  │
  │── WS Message ────────►  websocket/message.js
  │    { cmd, payload }       Command routing:
  │                             injectTouch  → normalizeTouchPayload → controller
  │                             injectScroll → normalizeScrollPayload → controller
  │                             injectKeyCode → controller
  │                             setVolume    → key injection sequence
  │                             audio_ready  → user.audioReady = true
  │
  │── WS Close ─────────►  websocket/close.js
  │                          abort active pipelines
  │                          evictPushedSerial (reset push cache)
  │                          user.client.close()
  │                          global.users.delete(id)
```

### 2. ADB Device Pool

`adb-tcp-service.js` maintains a pool of ADB connections keyed by `host:port`
(i.e. per ADB server). Connections are created lazily on first use and refreshed
on a configurable interval (`ADB_REFRESH_INTERVAL_MS`). Pool mutations are
serialized through an async mutex to prevent race conditions.

### 3. Audio Packet Pipeline

```
scrcpy AudioStream
  │── audioStream (Promise) ── withTimeout(8000) ──►
  │                                                  metadata switch:
  │   ┌─ disabled → log
  │   ├─ errored  → notify client
  │   └─ success  → audioPacketStream
  │
  │    audioPacketStream.pipeTo(WritableStream)
  │       ├─ extractAudioBuffer (unwrap packet wrappers)
  │       ├─ validateAudioPacket (codec-specific checks)
  │       ├─ sendToUser → WebSocket.send
  │       └─ backpressure guard (DROP_ABORT_THRESHOLD)
```

## Environment Variables

See `source/.env.example` for a full list with descriptions.

| Variable                  | Default  | Description |
|---------------------------|----------|-------------|
| `PORT`                    | `4001`   | Server listen port |
| `HOST`                    | `0.0.0.0` | Server listen address |
| `SSL`                     | `false`  | Enable HTTPS |
| `AUTH_ENABLED`            | `false`  | Enable HTTP Basic auth |
| `AUTH_USER`               | —        | Basic auth username |
| `AUTH_PASS`               | —        | Basic auth password |
| `LOG_LEVEL`               | `info`   | pino log level |
| `ADB_SERVER_LIST`         | —        | Comma-separated `host:port` ADB server addresses |
| `ADB_REFRESH_INTERVAL_MS` | `30000`  | ADB pool refresh interval |
| `UPLOAD_MAX_BYTES`        | `524288000` | APK upload size limit |
| `CORS_ORIGIN`             | `*`      | CORS `Access-Control-Allow-Origin` value |

## Technology Stack

| Concern        | Library |
|----------------|---------|
| HTTP / WS server | [uWebSockets.js](https://github.com/uNetworking/uWebSockets.js) |
| Serialization  | [msgpackr](https://github.com/kriszyp/msgpackr) |
| Logging        | [pino](https://getpino.io/) |
| ADB client     | [@yume-chan/adb](https://github.com/yume-chan/ya-webadb) |
| scrcpy client  | [@yume-chan/adb-scrcpy](https://github.com/yume-chan/ya-webadb) |
| Process manager | [PM2](https://pm2.keymetrics.io/) |

## Testing

Unit tests live in `source/tests/` and use the built-in `node:test` framework
(no external test runner required).

```sh
cd adb_streaming_service/source
pnpm install
node --test tests/*.test.js
```
