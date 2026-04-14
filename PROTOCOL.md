# FMD-AECS WebSocket Protocol

This document describes the message protocol used between the FMD-AECS streaming service and its clients.

## Transport

All messages are exchanged over a single WebSocket connection per client session.
Messages are encoded with [MessagePack](https://msgpack.org/) using the `msgpackr` library.

## Connection Handshake

### Client → Server (HTTP Upgrade)

```
GET ws://<host>:<port>/*?id=<sessionId>&device=<deviceSpec>[&<options>]
```

**Required query parameters:**

| Parameter | Type   | Description |
|-----------|--------|-------------|
| `id`      | string | Unique session identifier (any non-empty string, e.g. a UUID) |

**Optional query parameters:**

| Parameter      | Type    | Default | Description |
|----------------|---------|---------|-------------|
| `device`       | string  | auto    | Device specifier: `host:port/serial` or bare `serial`. Auto-selected when exactly one device is connected. |
| `audio`        | boolean | `true`  | Request audio stream |
| `audioCodec`   | string  | `raw`   | Audio codec hint: `raw`, `aac`, `opus` |
| `audioEncoder` | string  | default | Explicit encoder name (passed to scrcpy) |
| `video`        | boolean | `true`  | Request video stream |
| `videoCodec`   | string  | `h264`  | Video codec: `h264`, `h265`, `av1` |
| `videoEncoder` | string  | default | Explicit encoder name (passed to scrcpy) |
| `videoBitRate` | number  | `4`     | Video bit-rate in Mbps |
| `displayId`    | number  | `0`     | Android display ID |
| `maxSize`      | number  | `1280`  | Maximum video dimension (width or height), in pixels |
| `maxFps`       | number  | `60`    | Maximum video frame rate |

**Authentication** (when `AUTH_ENABLED=true`):  
The upgrade request must carry a valid HTTP Basic `Authorization` header, or an `auth` query parameter with a base64-encoded `user:pass` payload.

---

## Server → Client Messages

All server messages are MessagePack objects with at least a `media` field.

### `video_metadata`

Sent once at the start of the session with the video stream properties.

```json
{ "media": "video_metadata", "packet": { "codec": "h264", "width": 1080, "height": 1920, ... } }
```

### `video`

Sent for each video frame.

```json
{ "media": "video", "packet": <binary-encoded-frame> }
```

### `audio_metadata`

Sent once when the audio stream starts successfully.

```json
{ "media": "audio_metadata", "packet": { "type": "success", "codecName": "raw", ... } }
```

### `audio`

Sent for each audio packet.

```json
{ "media": "audio", "packet": <binary-encoded-audio-packet> }
```

### `audio_error`

Sent when the audio stream encounters an error.

```json
{ "media": "audio_error", "message": "audio stream errored" }
```

### `audio_invalid`

Sent when an audio packet fails codec-specific validation.

```json
{ "media": "audio_invalid", "reason": "aac_missing_adts_syncword", "details": { ... } }
```

### `audio_backpressure`

Sent when the server aborts the audio stream due to excessive client-side backpressure.

```json
{ "media": "audio_backpressure", "message": "server aborting audio stream due to backpressure", "dropped": 200 }
```

### `message`

Generic application-level message (e.g. clipboard events).

```json
{ "media": "message", "message": "clipboard text here" }
{ "media": "message", "type": "volume_set", "payload": { "ok": true, "action": "up" } }
```

### `error`

Sent when an error occurs that does not close the connection.

```json
{ "media": "error", "code": "audio_stream_failed", "message": "...", "error": { ... } }
```

---

## Client → Server Messages

All client messages are MessagePack objects with `cmd` and `payload` fields.

```json
{ "cmd": "<command>", "payload": <command-specific> }
```

### Touch / Pointer

**Command:** `injectTouch` (aliases: `injectPointer`, `injectMouse`, `pointer`)

```json
{
  "cmd": "injectTouch",
  "payload": {
    "action": 0,
    "pointerId": 1,
    "deviceX": 540,
    "deviceY": 960,
    "displayWidth": 1080,
    "displayHeight": 1920,
    "pressure": 1.0,
    "buttons": 1
  }
}
```

`action` values follow Android `MotionEvent`:
- `0` = ACTION_DOWN
- `1` = ACTION_UP
- `2` = ACTION_MOVE

An array payload triggers multiple touch events in sequence.

### Scroll

**Command:** `injectScroll`

```json
{
  "cmd": "injectScroll",
  "payload": {
    "x": 540,
    "y": 960,
    "scrollX": 0,
    "scrollY": -120,
    "displayWidth": 1080,
    "displayHeight": 1920
  }
}
```

### Key Events

**Command:** `injectKeyCode`

```json
{ "cmd": "injectKeyCode", "payload": { "action": 0, "keyCode": 3 } }
```

Named shorthand:

```json
{ "cmd": "injectKeyCode", "payload": "HOME" }
{ "cmd": "injectKeyCode", "payload": { "key": "BACK" } }
```

Supported named keys: `HOME`, `BACK`, `CALL`, `ENDCALL`, `POWER`, `VOLUME_UP`, `VOLUME_DOWN`,
`MUTE`, `MENU`, `APP_SWITCH`, `ENTER`, `DPAD_UP`, `DPAD_DOWN`, `DPAD_LEFT`, `DPAD_RIGHT`,
`DPAD_CENTER`, `SPACE`, `TAB`, `ESCAPE`.

### Volume Control

**Command:** `setVolume` (alias: `setDeviceVolume`, `setvolume`)

```json
{ "cmd": "setVolume", "payload": "up" }
{ "cmd": "setVolume", "payload": "down" }
{ "cmd": "setVolume", "payload": "mute" }
{ "cmd": "setVolume", "payload": { "delta": 3 } }
{ "cmd": "setVolume", "payload": { "volume": 0.7 } }
{ "cmd": "setVolume", "payload": { "muted": true } }
```

### Screen Power Mode

**Command:** `setScreenPowerMode`

```json
{ "cmd": "setScreenPowerMode", "payload": 2 }
```

### Rotate Device

**Command:** `rotateDevice`

```json
{ "cmd": "rotateDevice" }
```

### Clipboard Paste

**Command:** `clipboardPaste`

```json
{ "cmd": "clipboardPaste", "payload": "text to paste" }
```

### Audio Ready Signal

Signal that the client is ready to receive audio packets.

```json
{ "cmd": "audio_ready" }
```

---

## Debug Endpoint

`GET /_debug/audio_stats`

Returns per-session audio statistics as JSON. Requires authentication when `AUTH_ENABLED=true`.

```json
{
  "<sessionId>": {
    "audioStats": { "sent": 142, "dropped": 0, "lastPacketSize": 512, "lastPacketTs": 1712345678901 },
    "connected": true,
    "device": null
  }
}
```

---

## Health Endpoint

`GET /api/health` — returns `200 OK`. No authentication required.
