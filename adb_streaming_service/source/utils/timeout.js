/**
 * Race a promise against a timeout.
 *
 * @param {Promise|Function} promiseOrFactory - A promise or a zero-arg factory that returns one.
 * @param {number} ms - Timeout in milliseconds (default 10 000).
 * @param {string} [name] - Human-readable operation name used in the timeout error message.
 * @returns {Promise}
 */
export function withTimeout(promiseOrFactory, ms = 10_000, name = 'operation') {
    try {
        const promise =
            typeof promiseOrFactory === 'function' ? promiseOrFactory() : promiseOrFactory;
        return Promise.race([
            promise,
            new Promise((_, reject) =>
                setTimeout(
                    () => reject(new Error(`${name} timed out after ${ms}ms`)),
                    ms,
                ),
            ),
        ]);
    } catch (e) {
        return Promise.reject(e);
    }
}
