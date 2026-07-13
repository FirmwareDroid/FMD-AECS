/**
 * Per-device failure tracking with exponential back-off.
 *
 * Devices that consistently fail to respond (e.g. scrcpy server cannot be
 * started, or getDisplays / getEncoders always times out) should not be
 * retried at full speed on every metainfo refresh.  These helpers maintain
 * a lightweight in-memory failure registry and compute the back-off window
 * so callers can decide whether to attempt the operation or skip it.
 *
 * All helpers are pure functions that accept an explicit Map so they are
 * easy to unit-test without any mocking.
 */

export const DEFAULT_BACKOFF_BASE_MS = 5_000;   // 5 s initial back-off
export const DEFAULT_BACKOFF_MAX_MS  = 30_000;  // cap at 30 secs

/**
 * Return the number of milliseconds that must still elapse before the device
 * identified by `serial` should be retried.  Returns 0 when no back-off is
 * in effect.
 *
 * @param {Map<string,{count:number,lastFailTs:number}>} failureMap
 * @param {string|null|undefined} serial
 * @param {number} [baseMs]
 * @param {number} [maxMs]
 * @returns {number}
 */
export function getDeviceBackoffRemaining(failureMap, serial, baseMs = DEFAULT_BACKOFF_BASE_MS, maxMs = DEFAULT_BACKOFF_MAX_MS) {
    if (!serial || !failureMap.has(serial)) return 0;
    const { count, lastFailTs } = failureMap.get(serial);
    const backoffMs = Math.min(baseMs * Math.pow(2, count - 1), maxMs);
    return Math.max(0, backoffMs - (Date.now() - lastFailTs));
}

/**
 * Record a failure for `serial`.  Returns the updated cumulative failure count.
 *
 * @param {Map<string,{count:number,lastFailTs:number}>} failureMap
 * @param {string|null|undefined} serial
 * @returns {number}
 */
export function recordDeviceFailure(failureMap, serial) {
    if (!serial) return 0;
    const prev = failureMap.get(serial) || { count: 0, lastFailTs: 0 };
    const next = { count: prev.count + 1, lastFailTs: Date.now() };
    failureMap.set(serial, next);
    return next.count;
}

/**
 * Clear the failure record on success so the device is not penalised on the
 * next invocation.
 *
 * @param {Map<string,{count:number,lastFailTs:number}>} failureMap
 * @param {string|null|undefined} serial
 */
export function clearDeviceFailure(failureMap, serial) {
    if (serial) failureMap.delete(serial);
}
