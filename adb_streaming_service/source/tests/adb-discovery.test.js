/**
 * Unit tests for source/services/adb/adb-discovery.js
 * Run with: node --test source/tests/adb-discovery.test.js
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
    ipv4ToInt,
    intToIpv4,
    netmaskToPrefixLength,
    isPrivateIpv4,
    enumerateSubnetHosts,
    getLocalSubnets,
    probePort,
    discoverAdbServers,
} from '../services/adb/adb-discovery.js';

// ─── ipv4ToInt ────────────────────────────────────────────────────────────────

describe('ipv4ToInt', () => {
    test('converts 0.0.0.0 to 0', () => {
        assert.equal(ipv4ToInt('0.0.0.0'), 0);
    });

    test('converts 255.255.255.255 to max uint32', () => {
        assert.equal(ipv4ToInt('255.255.255.255'), 0xffffffff);
    });

    test('converts 192.168.1.1 correctly', () => {
        // 192*2^24 + 168*2^16 + 1*2^8 + 1
        assert.equal(ipv4ToInt('192.168.1.1'), (192 << 24 | 168 << 16 | 1 << 8 | 1) >>> 0);
    });

    test('throws for too few octets', () => {
        assert.throws(() => ipv4ToInt('192.168.1'), /Invalid/);
    });

    test('throws for non-numeric octet', () => {
        assert.throws(() => ipv4ToInt('192.168.1.abc'), /Invalid/);
    });

    test('throws for octet out of range', () => {
        assert.throws(() => ipv4ToInt('192.168.1.256'), /Invalid/);
    });

    test('throws for non-string input', () => {
        assert.throws(() => ipv4ToInt(12345), /Invalid/);
    });
});

// ─── intToIpv4 ────────────────────────────────────────────────────────────────

describe('intToIpv4', () => {
    test('converts 0 to 0.0.0.0', () => {
        assert.equal(intToIpv4(0), '0.0.0.0');
    });

    test('converts 0xffffffff to 255.255.255.255', () => {
        assert.equal(intToIpv4(0xffffffff), '255.255.255.255');
    });

    test('round-trips with ipv4ToInt', () => {
        const ip = '10.20.30.40';
        assert.equal(intToIpv4(ipv4ToInt(ip)), ip);
    });
});

// ─── netmaskToPrefixLength ────────────────────────────────────────────────────

describe('netmaskToPrefixLength', () => {
    test('255.255.255.0 → 24', () => {
        assert.equal(netmaskToPrefixLength('255.255.255.0'), 24);
    });

    test('255.255.0.0 → 16', () => {
        assert.equal(netmaskToPrefixLength('255.255.0.0'), 16);
    });

    test('255.0.0.0 → 8', () => {
        assert.equal(netmaskToPrefixLength('255.0.0.0'), 8);
    });

    test('255.255.255.255 → 32', () => {
        assert.equal(netmaskToPrefixLength('255.255.255.255'), 32);
    });

    test('0.0.0.0 → 0', () => {
        assert.equal(netmaskToPrefixLength('0.0.0.0'), 0);
    });

    test('non-string input returns 32', () => {
        assert.equal(netmaskToPrefixLength(null), 32);
        assert.equal(netmaskToPrefixLength(24), 32);
    });

    test('invalid string returns 32', () => {
        assert.equal(netmaskToPrefixLength('not.a.mask'), 32);
    });
});

// ─── isPrivateIpv4 ────────────────────────────────────────────────────────────

describe('isPrivateIpv4', () => {
    test('10.x.x.x is private', () => {
        assert.ok(isPrivateIpv4('10.0.0.1'));
        assert.ok(isPrivateIpv4('10.255.255.254'));
    });

    test('172.16.x.x to 172.31.x.x is private', () => {
        assert.ok(isPrivateIpv4('172.16.0.1'));
        assert.ok(isPrivateIpv4('172.31.255.254'));
    });

    test('172.15.x.x is not private', () => {
        assert.ok(!isPrivateIpv4('172.15.255.255'));
    });

    test('172.32.x.x is not private', () => {
        assert.ok(!isPrivateIpv4('172.32.0.0'));
    });

    test('192.168.x.x is private', () => {
        assert.ok(isPrivateIpv4('192.168.0.1'));
        assert.ok(isPrivateIpv4('192.168.255.254'));
    });

    test('192.169.x.x is not private', () => {
        assert.ok(!isPrivateIpv4('192.169.0.1'));
    });

    test('loopback is not considered private by isPrivateIpv4', () => {
        assert.ok(!isPrivateIpv4('127.0.0.1'));
    });

    test('public addresses are not private', () => {
        assert.ok(!isPrivateIpv4('8.8.8.8'));
        assert.ok(!isPrivateIpv4('1.1.1.1'));
    });

    test('invalid input returns false', () => {
        assert.ok(!isPrivateIpv4('not-an-ip'));
        assert.ok(!isPrivateIpv4(''));
    });
});

// ─── enumerateSubnetHosts ─────────────────────────────────────────────────────

describe('enumerateSubnetHosts', () => {
    test('returns 253 hosts for a /24 subnet (own IP excluded)', () => {
        const hosts = enumerateSubnetHosts('192.168.1.5', '255.255.255.0');
        assert.equal(hosts.length, 253);
        assert.ok(!hosts.includes('192.168.1.5'), 'own IP must be excluded');
        assert.ok(!hosts.includes('192.168.1.0'), 'network address must be excluded');
        assert.ok(!hosts.includes('192.168.1.255'), 'broadcast must be excluded');
    });

    test('excludes own IP from a /24 result', () => {
        const ownIp = '10.0.0.10';
        const hosts = enumerateSubnetHosts(ownIp, '255.255.255.0');
        assert.ok(!hosts.includes(ownIp));
    });

    test('clamps a /16 subnet to /24 (at most 254 hosts)', () => {
        // /16 would be 65534 hosts; clamping to /24 yields 254
        const hosts = enumerateSubnetHosts('172.16.0.5', '255.255.0.0');
        assert.ok(hosts.length <= 254, `expected ≤254 hosts for clamped /16, got ${hosts.length}`);
        assert.equal(hosts.length, 253); // 254 - 1 own IP
    });

    test('returns empty array for /31 (fewer than 2 usable hosts)', () => {
        const hosts = enumerateSubnetHosts('192.168.1.1', '255.255.255.254');
        assert.deepEqual(hosts, []);
    });

    test('returns empty array for /32', () => {
        const hosts = enumerateSubnetHosts('192.168.1.1', '255.255.255.255');
        assert.deepEqual(hosts, []);
    });

    test('returns empty array for invalid own IP', () => {
        const hosts = enumerateSubnetHosts('not-an-ip', '255.255.255.0');
        assert.deepEqual(hosts, []);
    });

    test('all returned addresses belong to the correct /24 network', () => {
        const hosts = enumerateSubnetHosts('192.168.1.100', '255.255.255.0');
        for (const h of hosts) {
            assert.ok(h.startsWith('192.168.1.'), `unexpected host ${h}`);
        }
    });
});

// ─── getLocalSubnets ─────────────────────────────────────────────────────────

describe('getLocalSubnets', () => {
    test('returns subnets for private IPv4 non-internal interfaces', () => {
        const mockIfaces = () => ({
            eth0: [
                { family: 'IPv4', internal: false, address: '10.0.0.5', netmask: '255.255.255.0' },
            ],
            lo: [
                { family: 'IPv4', internal: true, address: '127.0.0.1', netmask: '255.0.0.0' },
            ],
        });
        const subnets = getLocalSubnets(mockIfaces);
        assert.equal(subnets.length, 1);
        assert.equal(subnets[0].address, '10.0.0.5');
    });

    test('excludes non-IPv4 entries', () => {
        const mockIfaces = () => ({
            eth0: [
                { family: 'IPv6', internal: false, address: '::1', netmask: 'ffff::' },
                { family: 'IPv4', internal: false, address: '192.168.1.2', netmask: '255.255.255.0' },
            ],
        });
        const subnets = getLocalSubnets(mockIfaces);
        assert.equal(subnets.length, 1);
        assert.equal(subnets[0].address, '192.168.1.2');
    });

    test('excludes public IPv4 addresses', () => {
        const mockIfaces = () => ({
            eth0: [
                { family: 'IPv4', internal: false, address: '8.8.8.8', netmask: '255.255.255.0' },
            ],
        });
        const subnets = getLocalSubnets(mockIfaces);
        assert.equal(subnets.length, 0);
    });

    test('returns empty array when getInterfaces throws', () => {
        const subnets = getLocalSubnets(() => { throw new Error('mock error'); });
        assert.deepEqual(subnets, []);
    });

    test('returns empty array when there are no interfaces', () => {
        const subnets = getLocalSubnets(() => ({}));
        assert.deepEqual(subnets, []);
    });
});

// ─── probePort ────────────────────────────────────────────────────────────────

describe('probePort', () => {
    /** Create a mock socket that emits the given event after a short delay. */
    function makeMockSocket(event, delayMs = 0) {
        const listeners = {};
        const socket = {
            once(ev, fn) { listeners[ev] = fn; return socket; },
            setTimeout() { return socket; },
            destroy() {},
        };
        setTimeout(() => { if (listeners[event]) listeners[event](); }, delayMs);
        return socket;
    }

    test('resolves true when socket emits "connect"', async () => {
        const result = await probePort('10.0.0.1', 5037, 500, () => makeMockSocket('connect'));
        assert.ok(result);
    });

    test('resolves false when socket emits "error"', async () => {
        const result = await probePort('10.0.0.1', 5037, 500, () => makeMockSocket('error'));
        assert.ok(!result);
    });

    test('resolves false when socket emits "timeout"', async () => {
        const result = await probePort('10.0.0.1', 5037, 500, () => makeMockSocket('timeout'));
        assert.ok(!result);
    });

    test('resolves false when createConnection throws', async () => {
        const result = await probePort('10.0.0.1', 5037, 500, () => { throw new Error('refused'); });
        assert.ok(!result);
    });

    test('resolves false on timeout (no events fired)', async () => {
        // Socket that never emits anything; rely on the setTimeout timer.
        const listeners = {};
        const silent = {
            once(ev, fn) { listeners[ev] = fn; return silent; },
            setTimeout() { return silent; },
            destroy() {},
        };
        const result = await probePort('10.0.0.1', 5037, 20, () => silent);
        assert.ok(!result);
    });
});

// ─── discoverAdbServers ───────────────────────────────────────────────────────

describe('discoverAdbServers', () => {
    /** Build a mock createConnection that marks `openHosts` as open. */
    function makeConnFactory(openHosts) {
        return (opts) => {
            const listeners = {};
            const socket = {
                once(ev, fn) { listeners[ev] = fn; return socket; },
                setTimeout() { return socket; },
                destroy() {},
            };
            // Emit 'connect' for open hosts, 'error' for all others.
            const event = openHosts.has(opts.host) ? 'connect' : 'error';
            setTimeout(() => { if (listeners[event]) listeners[event](); }, 0);
            return socket;
        };
    }

    /** No-op logger. */
    const noop = { info: () => {}, debug: () => {}, error: () => {} };

    test('returns empty array when no subnets are found', async () => {
        const result = await discoverAdbServers({
            getInterfaces: () => ({}),
            createConnection: makeConnFactory(new Set()),
            logger: noop,
        });
        assert.deepEqual(result, []);
    });

    test('returns discovered servers that have an open port', async () => {
        const openHosts = new Set(['10.0.0.2', '10.0.0.3']);
        const result = await discoverAdbServers({
            port: 5037,
            getInterfaces: () => ({
                eth0: [{ family: 'IPv4', internal: false, address: '10.0.0.1', netmask: '255.255.255.0' }],
            }),
            createConnection: makeConnFactory(openHosts),
            logger: noop,
        });
        assert.ok(result.includes('10.0.0.2:5037'), `expected 10.0.0.2:5037 in ${result}`);
        assert.ok(result.includes('10.0.0.3:5037'), `expected 10.0.0.3:5037 in ${result}`);
    });

    test('excludes own container IP from results', async () => {
        // Even if the probe succeeds for 10.0.0.1, it must not appear — it is
        // the container's own address and is never probed.
        const openHosts = new Set(['10.0.0.1']);
        const result = await discoverAdbServers({
            port: 5037,
            getInterfaces: () => ({
                eth0: [{ family: 'IPv4', internal: false, address: '10.0.0.1', netmask: '255.255.255.0' }],
            }),
            createConnection: makeConnFactory(openHosts),
            logger: noop,
        });
        assert.ok(!result.includes('10.0.0.1:5037'), 'own IP must not appear in results');
    });

    test('returns sorted results', async () => {
        const openHosts = new Set(['10.0.0.5', '10.0.0.2', '10.0.0.9']);
        const result = await discoverAdbServers({
            port: 5037,
            getInterfaces: () => ({
                eth0: [{ family: 'IPv4', internal: false, address: '10.0.0.1', netmask: '255.255.255.0' }],
            }),
            createConnection: makeConnFactory(openHosts),
            logger: noop,
        });
        const sorted = [...result].sort();
        assert.deepEqual(result, sorted, 'results must be sorted');
    });

    test('respects additionalSubnets option', async () => {
        const openHosts = new Set(['192.168.99.10']);
        const result = await discoverAdbServers({
            port: 5037,
            // No real interfaces — only the additional subnet will be scanned.
            getInterfaces: () => ({}),
            additionalSubnets: ['192.168.99.1 255.255.255.0'],
            createConnection: makeConnFactory(openHosts),
            logger: noop,
        });
        assert.ok(result.includes('192.168.99.10:5037'));
    });

    test('ignores invalid additionalSubnets entries', async () => {
        const result = await discoverAdbServers({
            port: 5037,
            getInterfaces: () => ({}),
            additionalSubnets: ['not-valid', '8.8.8.8/255.255.255.0', null, 42],
            createConnection: makeConnFactory(new Set()),
            logger: noop,
        });
        // Public address 8.8.8.8 must be rejected (not private); nothing to scan.
        assert.deepEqual(result, []);
    });

    test('falls back to DEFAULT_ADB_PORT for invalid port option', async () => {
        // With port=-1 (invalid) the implementation must use the default (5037).
        // We open port 5037 on a host; if the default is used, it will appear.
        const openHosts = new Set(['10.0.0.2']);
        const connectedPorts = [];
        const result = await discoverAdbServers({
            port: -1,
            getInterfaces: () => ({
                eth0: [{ family: 'IPv4', internal: false, address: '10.0.0.1', netmask: '255.255.255.0' }],
            }),
            createConnection: (opts) => {
                connectedPorts.push(opts.port);
                const listeners = {};
                const socket = {
                    once(ev, fn) { listeners[ev] = fn; return socket; },
                    setTimeout() { return socket; },
                    destroy() {},
                };
                const event = openHosts.has(opts.host) ? 'connect' : 'error';
                setTimeout(() => { if (listeners[event]) listeners[event](); }, 0);
                return socket;
            },
            logger: noop,
        });
        // All probes must have used port 5037 (the default).
        assert.ok(connectedPorts.every(p => p === 5037), `expected all probes on port 5037, got: ${[...new Set(connectedPorts)]}`);
        assert.ok(result.includes('10.0.0.2:5037'));
    });

    test('returns empty array when no hosts have the port open', async () => {
        const result = await discoverAdbServers({
            port: 5037,
            getInterfaces: () => ({
                eth0: [{ family: 'IPv4', internal: false, address: '192.168.1.1', netmask: '255.255.255.0' }],
            }),
            createConnection: makeConnFactory(new Set()),
            logger: noop,
        });
        assert.deepEqual(result, []);
    });
});
