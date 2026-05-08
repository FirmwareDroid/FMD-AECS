/**
 * ADB Server Auto-Discovery
 *
 * Probes every host address on the container's local subnet(s) for an open TCP
 * connection on the ADB server port (default: 5037).  Intended for Docker
 * environments where multiple ADB server containers share the same bridge or
 * overlay network.
 *
 * Design constraints
 * - Only private (RFC 1918) address ranges are ever scanned — no accidental
 *   probing of external hosts.
 * - Subnet scan is clamped to /24 (at most 254 hosts per interface) to keep
 *   the scan fast and the blast radius small.
 * - All networking primitives are injectable so the module is fully unit-testable
 *   without a real network.
 * - No module-level side effects; all state is local to each function call.
 */

import net from 'node:net';
import os from 'node:os';

// ─── Constants ──────────────────────────────────────────────────────────────

const DEFAULT_ADB_PORT = 5037;
// Per-host TCP connection timeout (ms). Shorter time reduces total scan time but
// needs to be high enough to tolerate transient packet loss on busy networks.
const DEFAULT_TIMEOUT_MS = 500;
// Maximum number of parallel TCP probes. Increase to scan faster on beefy hosts.
const DEFAULT_MAX_CONCURRENT = 200;
// Handshake defaults: attempt a minimal ADB "CNXN" handshake after TCP connect
// to reduce false-positives from non-ADB services listening on the same port.
const DEFAULT_HANDSHAKE_ENABLED = true;
const DEFAULT_HANDSHAKE_ATTEMPTS = 2;
const DEFAULT_HANDSHAKE_TIMEOUT_MS = 1000;
const DEFAULT_HANDSHAKE_BACKOFF_MS = 200;
// Smallest prefix we will enumerate (/24 = 254 hosts). Wider subnets are
// clamped to this value so a single Docker /16 does not cause 65 k probes.
const MIN_PREFIX_LENGTH = 24;

// ─── Pure IPv4 helpers ───────────────────────────────────────────────────────

/**
 * Convert a dotted-decimal IPv4 string to an unsigned 32-bit integer.
 *
 * @param {string} ip
 * @returns {number}
 * @throws {RangeError} if the input is not a valid IPv4 address.
 */
export function ipv4ToInt(ip) {
    if (typeof ip !== 'string') throw new RangeError(`Invalid IPv4 address: ${ip}`);
    const parts = ip.split('.');
    if (parts.length !== 4) throw new RangeError(`Invalid IPv4 address: ${ip}`);
    let result = 0;
    for (const part of parts) {
        const n = parseInt(part, 10);
        if (Number.isNaN(n) || n < 0 || n > 255 || part.trim() !== String(n)) {
            throw new RangeError(`Invalid IPv4 address: ${ip}`);
        }
        result = ((result << 8) | n) >>> 0;
    }
    return result;
}

/**
 * Convert an unsigned 32-bit integer to a dotted-decimal IPv4 string.
 *
 * @param {number} n
 * @returns {string}
 */
export function intToIpv4(n) {
    return [
        (n >>> 24) & 0xff,
        (n >>> 16) & 0xff,
        (n >>> 8) & 0xff,
        n & 0xff,
    ].join('.');
}

/**
 * Convert a dotted-decimal netmask (e.g. "255.255.255.0") to a CIDR prefix
 * length (e.g. 24).  Returns 32 for any invalid input.
 *
 * @param {string} netmask
 * @returns {number}
 */
export function netmaskToPrefixLength(netmask) {
    if (typeof netmask !== 'string') return 32;
    const parts = netmask.split('.');
    if (parts.length !== 4) return 32;
    let bits = 0;
    for (const part of parts) {
        const n = parseInt(part, 10);
        if (Number.isNaN(n) || n < 0 || n > 255) return 32;
        // count set bits
        let byte = n;
        while (byte > 0) {
            bits += byte & 1;
            byte >>>= 1;
        }
    }
    return bits;
}

/**
 * Return true if `ip` falls within a private (RFC 1918) IPv4 range.
 * Loopback (127.x.x.x) addresses are intentionally excluded — they are
 * not cross-container addresses and are skipped during discovery.
 *
 * @param {string} ip
 * @returns {boolean}
 */
export function isPrivateIpv4(ip) {
    try {
        const n = ipv4ToInt(ip);
        // JavaScript's bitwise & returns a signed 32-bit integer, so we apply
        // >>> 0 to obtain an unsigned value before comparing with hex literals.

        // 10.0.0.0/8
        if (((n & 0xff000000) >>> 0) === 0x0a000000) return true;
        // 172.16.0.0/12
        if (((n & 0xfff00000) >>> 0) === 0xac100000) return true;
        // 192.168.0.0/16
        if (((n & 0xffff0000) >>> 0) === 0xc0a80000) return true;
        return false;
    } catch {
        return false;
    }
}

/**
 * Enumerate all host addresses in a given subnet, excluding the network
 * address, broadcast address, and the container's own address.
 *
 * The effective prefix length is clamped to `MIN_PREFIX_LENGTH` (24) so that
 * scanning a Docker /16 or /12 network never generates more than 254 entries
 * per call.  Subnets narrower than /30 are skipped (fewer than 2 usable hosts).
 *
 * @param {string} ownIp   - Container's IP on this interface.
 * @param {string} netmask - Dotted-decimal netmask.
 * @returns {string[]}     - Host IPs to probe (own IP excluded).
 */
export function enumerateSubnetHosts(ownIp, netmask) {
    const rawPrefix = netmaskToPrefixLength(netmask);
    // Clamp: never scan wider than /MIN_PREFIX_LENGTH to keep the probe set small.
    const prefixLen = Math.max(rawPrefix, MIN_PREFIX_LENGTH);
    // /31 has 0 usable hosts by conventional standards; /30 has 2.
    if (prefixLen > 30) return [];

    const maskInt = ~((1 << (32 - prefixLen)) - 1) >>> 0;
    let ownInt;
    try {
        ownInt = ipv4ToInt(ownIp);
    } catch {
        return [];
    }
    const networkInt = (ownInt & maskInt) >>> 0;
    const broadcastInt = (networkInt | (~maskInt >>> 0)) >>> 0;

    const hosts = [];
    for (let addr = networkInt + 1; addr < broadcastInt; addr++) {
        if (addr === ownInt) continue;
        hosts.push(intToIpv4(addr));
    }
    return hosts;
}

// ─── Network helpers ─────────────────────────────────────────────────────────

/**
 * Return the container's local private IPv4 subnet descriptors, derived from
 * `os.networkInterfaces()`.  Loopback and non-IPv4 entries are excluded.
 *
 * @param {() => object} [getInterfaces] - Injectable; defaults to os.networkInterfaces.
 * @returns {{ address: string, netmask: string }[]}
 */
export function getLocalSubnets(getInterfaces = os.networkInterfaces) {
    const subnets = [];
    try {
        const ifaces = getInterfaces();
        for (const addrs of Object.values(ifaces)) {
            if (!Array.isArray(addrs)) continue;
            for (const addr of addrs) {
                if (addr.family !== 'IPv4' || addr.internal) continue;
                if (!isPrivateIpv4(addr.address)) continue;
                subnets.push({ address: addr.address, netmask: addr.netmask });
            }
        }
    } catch {
        // best-effort; caller handles empty result
    }
    return subnets;
}

/**
 * Attempt a TCP connection to `host:port`.  Resolves to `true` when the
 * connection is established within `timeoutMs`; resolves to `false` for any
 * error or timeout.  The socket is always destroyed before resolution.
 *
 * @param {string}   host
 * @param {number}   port
 * @param {number}   timeoutMs
 * @param {Function} [createConnection] - Injectable; defaults to net.createConnection.
 * @returns {Promise<boolean>}
 */
export function probePort(host, port, timeoutMs, createConnection = net.createConnection, attempts = 1, backoffMs = 100) {
    // Attempts: number of TCP connect attempts to try before giving up.
    // backoffMs: delay between attempts when a connect fails.
    return new Promise((resolve) => {
        let attempt = 0;
        let settled = false;

        const tryOnce = () => {
            if (settled) return;
            attempt += 1;

            let socket;
            try {
                socket = createConnection({ host, port, allowHalfOpen: false });
            } catch {
                if (attempt >= attempts) return resolve(false);
                setTimeout(tryOnce, backoffMs);
                return;
            }

            const onDone = (result) => {
                if (settled) return;
                settled = true;
                try { socket.destroy(); } catch (_) { /* ignore */ }
                resolve(result);
            };

            const timer = setTimeout(() => onDone(false), timeoutMs);
            socket.once('connect', () => { clearTimeout(timer); onDone(true); });
            socket.once('error', () => { clearTimeout(timer); if (attempt >= attempts) onDone(false); else setTimeout(tryOnce, backoffMs); });
            socket.once('timeout', () => { clearTimeout(timer); if (attempt >= attempts) onDone(false); else setTimeout(tryOnce, backoffMs); });
            socket.setTimeout(timeoutMs);
        };

        tryOnce();
    });
}

// ─── Minimal ADB handshake probe ────────────────────────────────────────────

/**
 * Compute CRC32 for a Buffer. Lightweight implementation (table-based).
 *
 * @param {Buffer} buf
 * @returns {number} unsigned 32-bit CRC
 */
function crc32(buf) {
    // generated on first call
    if (!crc32.table) {
        const table = new Uint32Array(256);
        for (let i = 0; i < 256; i++) {
            let t = i;
            for (let j = 0; j < 8; j++) {
                if (t & 1) t = 0xedb88320 ^ (t >>> 1);
                else t = t >>> 1;
            }
            table[i] = t >>> 0;
        }
        crc32.table = table;
    }
    let crc = 0xffffffff;
    const table = crc32.table;
    for (let i = 0; i < buf.length; i++) {
        crc = (crc >>> 8) ^ table[(crc ^ buf[i]) & 0xff];
    }
    return (crc ^ 0xffffffff) >>> 0;
}

/**
 * Perform a minimal ADB CNXN handshake to confirm an ADB server is speaking
 * the ADB protocol on the other end of an open TCP socket. Returns true when
 * a syntactically valid ADB response header is received.
 *
 * This function is intentionally conservative: it only validates the response
 * header magic/structure and does not implement the full protocol state machine.
 *
 * @param {string} host
 * @param {number} port
 * @param {number} timeoutMs - per-attempt timeout for the whole handshake
 * @param {Function} createConnection - injectable net.createConnection
 * @param {number} attempts - number of handshake attempts
 * @param {number} backoffMs - delay between attempts
 * @returns {Promise<boolean>}
 */
export function probeAdbHandshake(host, port, timeoutMs, createConnection = net.createConnection, attempts = 1, backoffMs = 100) {
    return new Promise((resolve) => {
        let attempt = 0;
        let settled = false;

        const tryOnce = () => {
            if (settled) return;
            attempt += 1;

            let socket;
            try {
                socket = createConnection({ host, port, allowHalfOpen: false });
            } catch (err) {
                if (attempt >= attempts) return resolve(false);
                return setTimeout(tryOnce, backoffMs);
            }

            const onDone = (result) => {
                if (settled) return;
                settled = true;
                try { socket.destroy(); } catch (_) {}
                resolve(result);
            };

            const timer = setTimeout(() => onDone(false), timeoutMs);

            socket.once('connect', () => {
                // build a minimal CNXN packet with no payload
                const dataBuf = Buffer.alloc(0);
                const cmdBuf = Buffer.from('CNXN', 'ascii');
                const cmd = cmdBuf.readUInt32LE(0) >>> 0;
                const arg0 = 0x01000000; // host version 0x01000000 (ADB protocol version 1)
                const arg1 = 4096; // max data payload
                const length = dataBuf.length >>> 0;
                const checksum = length ? crc32(dataBuf) : 0;
                const magic = (cmd ^ 0xffffffff) >>> 0;

                const header = Buffer.alloc(24);
                header.writeUInt32LE(cmd, 0);
                header.writeUInt32LE(arg0, 4);
                header.writeUInt32LE(arg1, 8);
                header.writeUInt32LE(length, 12);
                header.writeUInt32LE(checksum, 16);
                header.writeUInt32LE(magic, 20);

                // send header (no payload) and wait for server response header
                socket.write(header);

                let acc = Buffer.alloc(0);
                const onData = (chunk) => {
                    acc = Buffer.concat([acc, chunk]);
                    if (acc.length >= 24) {
                        clearTimeout(timer);
                        socket.removeListener('data', onData);
                        // parse header
                        try {
                            const rcmd = acc.readUInt32LE(0) >>> 0;
                            const rarg0 = acc.readUInt32LE(4) >>> 0;
                            const rarg1 = acc.readUInt32LE(8) >>> 0;
                            const rlen = acc.readUInt32LE(12) >>> 0;
                            const rck = acc.readUInt32LE(16) >>> 0;
                            const rmagic = acc.readUInt32LE(20) >>> 0;
                            const expectedMagic = (rcmd ^ 0xffffffff) >>> 0;
                            // validate magic and reasonable payload length
                            if (rmagic === expectedMagic && rlen < 16 * 1024) {
                                onDone(true);
                                return;
                            }
                        } catch (e) {
                            // fall through to failure
                        }
                        onDone(false);
                    }
                };

                socket.on('data', onData);
                socket.once('error', () => { clearTimeout(timer); socket.removeListener('data', onData); if (attempt >= attempts) onDone(false); else setTimeout(tryOnce, backoffMs); });
                socket.once('timeout', () => { clearTimeout(timer); socket.removeListener('data', onData); if (attempt >= attempts) onDone(false); else setTimeout(tryOnce, backoffMs); });
                socket.setTimeout(timeoutMs);
            });

            socket.once('error', () => {
                clearTimeout(timer);
                if (attempt >= attempts) onDone(false); else setTimeout(tryOnce, backoffMs);
            });

            socket.once('timeout', () => {
                clearTimeout(timer);
                if (attempt >= attempts) onDone(false); else setTimeout(tryOnce, backoffMs);
            });
        };

        tryOnce();
    });
}

// ─── Concurrency helper ───────────────────────────────────────────────────────

/**
 * Map `fn` over `items` with at most `limit` concurrent executions.
 *
 * @template T, R
 * @param {T[]}               items
 * @param {number}            limit
 * @param {(item: T) => Promise<R>} fn
 * @returns {Promise<R[]>}
 */
async function pooledMap(items, limit, fn) {
    const results = new Array(items.length);
    let index = 0;

    async function worker() {
        while (index < items.length) {
            const i = index++;
            results[i] = await fn(items[i]);
        }
    }

    const concurrency = Math.min(limit, items.length);
    if (concurrency === 0) return results;
    await Promise.all(Array.from({ length: concurrency }, worker));
    return results;
}

// ─── Public API ───────────────────────────────────────────────────────────────

/**
 * Discover ADB servers reachable on the container's Docker network.
 *
 * The function scans every host address on each local private IPv4 subnet
 * (clamped to /24) for an open TCP port.  Discovered endpoints are returned as
 * `"host:port"` strings ready for use in `ADB_SERVER_LIST`.
 *
 * All options have safe defaults and the function is fully non-destructive —
 * by default it performs only TCP probes. Optionally a minimal ADB CNXN
 * handshake can be enabled to confirm the remote endpoint is a real ADB server.
 *
 * @param {object}   [options]
 * @param {number}   [options.port=5037]
 *   ADB server port to probe.
 * @param {number}   [options.timeoutMs=500]
 *   Per-host TCP connection timeout in milliseconds.
 * @param {number}   [options.maxConcurrent=50]
 *   Maximum number of parallel TCP probes.
 * @param {string[]} [options.additionalSubnets]
 *   Extra subnets to scan in `"address netmask"` or `"address/netmask"` format
 *   (e.g. `["10.0.0.1 255.255.255.0"]`).  Only entries with valid private IPv4
 *   addresses and netmasks are used.
 * @param {object}   [options.logger]
 *   Logger with `info`, `debug`, and `error` methods.  Defaults to a no-op logger.
 * @param {Function} [options.getInterfaces]
 *   Injectable replacement for `os.networkInterfaces` (for testing).
 * @param {Function} [options.createConnection]
 *   Injectable replacement for `net.createConnection` (for testing).
 * @returns {Promise<string[]>}  Sorted array of `"host:port"` strings.
 */
export async function discoverAdbServers({
    port = DEFAULT_ADB_PORT,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    maxConcurrent = DEFAULT_MAX_CONCURRENT,
    additionalSubnets = [],
    logger = null,
    getInterfaces = os.networkInterfaces,
    createConnection = net.createConnection,
    // tuning options
    samplePerSubnet = 50,
    neighborWindow = 8,
    handshakeEnabled = DEFAULT_HANDSHAKE_ENABLED,
    handshakeAttempts = DEFAULT_HANDSHAKE_ATTEMPTS,
    handshakeTimeoutMs = DEFAULT_HANDSHAKE_TIMEOUT_MS,
    handshakeBackoffMs = DEFAULT_HANDSHAKE_BACKOFF_MS,
} = {}) {
    const log = logger ?? { info: () => {}, debug: () => {}, error: () => {} };

    // Validate and sanitize numeric inputs.
    const validPort = Number.isInteger(port) && port > 0 && port <= 65535
        ? port : DEFAULT_ADB_PORT;
    const validTimeout = Number.isFinite(timeoutMs) && timeoutMs > 0
        ? Math.round(timeoutMs) : DEFAULT_TIMEOUT_MS;
    const validConcurrency = Number.isInteger(maxConcurrent) && maxConcurrent > 0
        ? maxConcurrent : DEFAULT_MAX_CONCURRENT;

    // Collect subnets from OS interfaces.
    const subnets = getLocalSubnets(getInterfaces);

    // Parse any caller-supplied extra subnets (format: "address/netmask" or "address netmask").
    if (Array.isArray(additionalSubnets)) {
        for (const entry of additionalSubnets) {
            if (typeof entry !== 'string') continue;
            const parts = entry.split(/[/\s]+/).filter(Boolean);
            if (parts.length < 2) continue;
            const [address, netmask] = parts;
            if (net.isIPv4(address) && net.isIPv4(netmask) && isPrivateIpv4(address)) {
                subnets.push({ address, netmask });
            }
        }
    }

    if (subnets.length === 0) {
        log.debug('ADB discovery: no suitable local subnets found; skipping scan');
        return [];
    }

    // Build a per-subnet candidate list so we can do two-phase probing: a
    // light sample scan and, if a host is found in a subnet, a full scan of
    // the remaining hosts in that subnet. This helps find additional devices
    // in the same network when at least one device is observed.
    const perSubnet = [];
    for (const { address, netmask } of subnets) {
        const hosts = enumerateSubnetHosts(address, netmask);
        log.debug(`ADB discovery: subnet ${address}/${netmask} → ${hosts.length} candidate(s)`);
        if (hosts.length === 0) continue;
        perSubnet.push({ address, netmask, hosts });
    }

    if (perSubnet.length === 0) {
        log.debug('ADB discovery: no candidate hosts to probe');
        return [];
    }

    log.info(`ADB discovery: probing ${perSubnet.length} subnet(s) on port ${validPort} (tcpTimeout=${validTimeout}ms, handshake=${handshakeEnabled}, concurrency=${validConcurrency})`);

    const reachable = [];
    const probed = new Set();

    // Phase 1: sample a small number of hosts per subnet to detect presence.
    const SAMPLE_PER_SUBNET = Math.max(1, Math.min(512, Number(samplePerSubnet) || 50));
    const sampleHosts = [];
    for (const s of perSubnet) {
        const { hosts } = s;
        if (hosts.length <= SAMPLE_PER_SUBNET) {
            for (const h of hosts) sampleHosts.push({ host: h, subnet: s });
        } else {
            // pick SAMPLE_PER_SUBNET evenly distributed hosts across the subnet
            for (let i = 0; i < SAMPLE_PER_SUBNET; i++) {
                const idx = Math.floor((i * hosts.length) / SAMPLE_PER_SUBNET);
                sampleHosts.push({ host: hosts[idx], subnet: s });
            }
        }
    }

    // Probe sampled hosts
    await pooledMap(sampleHosts, validConcurrency, async (entry) => {
        const host = entry.host;
        if (probed.has(host)) return;
        const open = await probePort(host, validPort, validTimeout, createConnection);
        probed.add(host);
        if (!open) return;
        if (handshakeEnabled) {
            const ok = await probeAdbHandshake(host, validPort, handshakeTimeoutMs, createConnection, handshakeAttempts, handshakeBackoffMs);
            if (!ok) return;
        }
        log.info(`ADB discovery: found ADB server at ${host}:${validPort} (sample)`);
        reachable.push(`${host}:${validPort}`);
        // mark this subnet for a full scan by setting a flag on the subnet object
        entry.subnet._found = true;
    });

    // Phase 2: for any subnet where we found at least one host, first probe a
    // small neighbor window around each found host (servers tend to cluster),
    // then if needed fall back to a full subnet scan.
    const NEIGHBOR_WINDOW = Math.max(1, Math.min(256, Number(neighborWindow) || 8)); // ±N addresses around found host
    const neighborTargets = [];
    for (const s of perSubnet) {
        if (!s._found) continue;
        // collect found hosts in this subnet (could be multiple)
        const foundHosts = s.hosts.filter((h) => reachable.find(r => r.startsWith(h + ':')));
        // if reachable didn't record by exact match, also check probed markers
        // but we'll generate neighbors around any host in s.hosts that was probed and succeeded
        for (const found of foundHosts) {
            try {
                const baseInt = ipv4ToInt(found);
                // generate neighbors
                for (let delta = -NEIGHBOR_WINDOW; delta <= NEIGHBOR_WINDOW; delta++) {
                    if (delta === 0) continue;
                    const addrInt = (baseInt + delta) >>> 0;
                    const addr = intToIpv4(addrInt);
                    // ensure addr belongs to this subnet's host list
                    if (s.hosts.includes(addr) && !probed.has(addr)) {
                        neighborTargets.push({ host: addr, subnet: s });
                    }
                }
            } catch (e) {
                // skip invalid conversion
            }
        }
    }

    if (neighborTargets.length > 0) {
        log.info(`ADB discovery: probing ${neighborTargets.length} neighbor host(s) around discovered servers`);
        await pooledMap(neighborTargets, validConcurrency, async (entry) => {
            const host = entry.host;
            if (probed.has(host)) return;
            const open = await probePort(host, validPort, validTimeout, createConnection, 2, 200);
            probed.add(host);
            if (!open) return;
            if (handshakeEnabled) {
                const ok = await probeAdbHandshake(host, validPort, handshakeTimeoutMs, createConnection, handshakeAttempts, handshakeBackoffMs);
                if (!ok) return;
            }
            log.info(`ADB discovery: found ADB server at ${host}:${validPort} (neighbor)`);
            reachable.push(`${host}:${validPort}`);
            entry.subnet._found = true;
        });
    }

    // After probing neighbors, prepare a full-scan list for subnets with any found hosts
    const toProbeFull = [];
    for (const s of perSubnet) {
        if (!s._found) continue;
        for (const h of s.hosts) {
            if (probed.has(h)) continue;
            toProbeFull.push({ host: h, subnet: s });
        }
    }

    if (toProbeFull.length > 0) {
        log.info(`ADB discovery: performing full scan of ${toProbeFull.length} host(s) in subnets with detected servers`);
        await pooledMap(toProbeFull, validConcurrency, async (entry) => {
            const host = entry.host;
            if (probed.has(host)) return;
            const open = await probePort(host, validPort, validTimeout, createConnection, 2, 200);
            probed.add(host);
            if (!open) return;
            if (handshakeEnabled) {
                const ok = await probeAdbHandshake(host, validPort, handshakeTimeoutMs, createConnection, handshakeAttempts, handshakeBackoffMs);
                if (!ok) return;
            }
            log.info(`ADB discovery: found ADB server at ${host}:${validPort}`);
            reachable.push(`${host}:${validPort}`);
        });
    }

    // If no reachable hosts were found at all in the sample+targeted full-scan,
    // fall back to scanning all candidates (this avoids missing servers when our
    // sampling strategy missed the only host in a subnet).
    if (reachable.length === 0) {
        log.info('ADB discovery: no servers found in sampling phase — falling back to full scan of all candidate hosts');
        const allHosts = [];
        for (const s of perSubnet) for (const h of s.hosts) if (!probed.has(h)) allHosts.push(h);
        if (allHosts.length > 0) {
            await pooledMap(allHosts, validConcurrency, async (host) => {
                if (probed.has(host)) return;
                const open = await probePort(host, validPort, validTimeout, createConnection);
                probed.add(host);
                if (!open) return;
                if (handshakeEnabled) {
                    const ok = await probeAdbHandshake(host, validPort, handshakeTimeoutMs, createConnection, handshakeAttempts, handshakeBackoffMs);
                    if (!ok) return;
                }
                log.info(`ADB discovery: found ADB server at ${host}:${validPort}`);
                reachable.push(`${host}:${validPort}`);
            });
        }
    }

    reachable.sort();
    log.info(`ADB discovery: scan complete — found ${reachable.length} ADB server(s)`);
    return reachable;
}
