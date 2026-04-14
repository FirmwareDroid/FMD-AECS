/**
 * ADB Device Monitor
 *
 * Proactively pushes the scrcpy server binary to every ADB device that is
 * connected to any of the configured ADB server pools.  The monitor runs an
 * initial push immediately when started and then re-checks on a fixed interval
 * so that newly attached devices are covered without waiting for a WebSocket
 * client to connect.
 *
 * All heavy dependencies are injected so the module is easy to unit-test.
 *
 * @param {object} deps
 * @param {object}   deps.logger      - Logger with info/debug/error methods.
 * @param {Function} deps.getPools    - Returns the current pool array: () => Pool[].
 * @param {Function} deps.Adb         - Constructor: new Adb(transport) → ADB instance.
 * @param {Function} deps.pushServer  - Async fn that pushes scrcpy binary: (adb) => Promise<void>.
 *
 * @returns {{ pushToAllDevices: Function, start: Function, stop: Function }}
 */
export function createDeviceMonitor({ logger, getPools, Adb, pushServer }) {
    /**
     * Push the scrcpy server binary to every device that is currently
     * connected across all pools.  Errors for individual devices are logged
     * and do not abort pushes to the remaining devices.
     */
    async function pushToAllDevices() {
        const pools = getPools();
        if (!pools || pools.length === 0) {
            logger.debug('Device monitor: no pools available, skipping push');
            return;
        }

        // Collect (pool, serial) pairs from every pool.
        // Only the first device returned per pool is used; this mirrors the
        // existing single-device-per-pool assumption in AdbTcpService.getDevices().
        const deviceTasks = [];
        for (const pool of pools) {
            try {
                const devices = await pool.client.getDevices();
                if (!Array.isArray(devices) || devices.length === 0) continue;
                const device = devices[0];
                if (!device || !device.serial) continue;
                deviceTasks.push({ pool, serial: device.serial });
            } catch (e) {
                logger.error(`Device monitor: getDevices failed for pool ${pool.key}: ${e?.message || e}`);
            }
        }

        if (deviceTasks.length === 0) {
            logger.debug('Device monitor: no devices connected');
            return;
        }

        // Push concurrently to all discovered devices.
        await Promise.all(deviceTasks.map(async ({ pool, serial }) => {
            try {
                const transport = await pool.client.createTransport({ serial });
                const adb = new Adb(transport);
                await pushServer(adb);
                logger.info(`Device monitor: pushed scrcpy server to serial=${serial} pool=${pool.key}`);
            } catch (e) {
                logger.error(`Device monitor: push failed for serial=${serial} pool=${pool.key}: ${e?.message || e}`);
            }
        }));
    }

    /**
     * Start the monitor: push immediately, then push on each tick of
     * `intervalMs`.  Returns the interval handle so the caller can stop it.
     *
     * @param {number} [intervalMs=30000]
     * @returns {ReturnType<typeof setInterval>}
     */
    function start(intervalMs = 30_000) {
        logger.info(`Device monitor starting (interval=${intervalMs}ms)`);
        pushToAllDevices().catch((e) =>
            logger.error(`Device monitor: initial push failed: ${e?.message || e}`)
        );
        const timer = setInterval(() => {
            pushToAllDevices().catch((e) =>
                logger.error(`Device monitor: periodic push failed: ${e?.message || e}`)
            );
        }, intervalMs);
        // Allow the Node.js event loop to exit even if the timer is still active.
        if (typeof timer.unref === 'function') timer.unref();
        return timer;
    }

    /**
     * Stop a previously started monitor timer.
     *
     * @param {ReturnType<typeof setInterval>} timer
     */
    function stop(timer) {
        if (timer) clearInterval(timer);
    }

    return { pushToAllDevices, start, stop };
}
