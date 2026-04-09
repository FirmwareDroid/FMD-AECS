import { logger } from "../../services/logger.js";
import * as uws from "uWebSockets.js";
import Route from "./route.js";

import readline from "node:readline";

class App {
	constructor(config = {}) {
		this.uws = uws;
		this.getParts = uws.getParts;
		this.config = config;
		// allow explicit protocol via config.protocol or env-driven SSL
		this.protocol = config.protocol || (process.env.SSL === "true" || process.env.SSL === "1" ? "https" : "http");
		this.host = config.host || "0.0.0.0";
		this.port = Number.parseInt(config.port || 4001);
		this.sslOptions = config.sslOptions || null;
		logger.info(`App protocol set to: ${this.protocol.toUpperCase()}`);
		logger.info(`SSL Options: ${JSON.stringify(config.sslOptions, null, 2)}`);
		// If HTTPS requested but no sslOptions provided, throw early to avoid insecure startup
		if (this.protocol === "https" && !this.sslOptions) {
			logger.error("HTTPS requested but no SSL options provided (key/cert path missing). Aborting startup.");
			throw new Error("Missing SSL options for HTTPS protocol");
		}
		logger.info("Setting up uWebSockets.js server...");
		try{
			this.server = this.protocol === "https" ? uws.SSLApp(config.sslOptions) : uws.App();
		}catch (err){
			logger.error("Failed to initialize uWebSockets.js server:", err);
			throw new Error("uWebSockets.js server initialization failed");
		}
		logger.info("uWebSockets.js server initialized.");
		this.logger = logger;
		this.token = null; // us_listen_socket
		this.hooks = new Map([["onclose", new Set()]]);
		this.handlers = new Set();
		this.routes = new Set();
		this.plugins = new Map();

		// initialize routes
		const routes = config.routes || [];
		logger.info(`Initializing ${routes.length} routes from config...`);
		if (routes?.length) {
			// biome-ignore lint/complexity/noForEach: <explanation>
			routes.forEach((route) => {
				this.route(route);
			});
		}
	}

	/**
	 * Register new route
	 * @param {New route options} r
	 */
	route(r, defaults) {
		logger.info(`Registering new route: ${r.method} ${r.url || ""}`);
		const route = new Route(
			{
				app: this,
				route: r,
				logger: this.logger,
			},
			defaults,
		);
		if (!route.method) {
			throw new Error("Route must have method");
		}
		if (route.method.toLowerCase() !== "ws") {
			if (!route.url || !route.handler) {
				throw new Error("Route must have method, url and handler defined!");
			}
		}

		this.routes.add(route);
		const handler =
			route.method.toLowerCase() === "ws"
				? route.wsHandler()
				: route.httpHandler();
		this.server[route.method.toLowerCase()](route.url, handler);
		return this;
	}

	/**
	 * Register new plugin
	 * @param {new addon/plugin} addon
	 */
	async plugin(addon) {
		await addon(this);
		return this;
	}

	/**
	 * Add new event handler
	 * @param {name of the event} name
	 * @param {callback as event handler} cb
	 */
	on(name, cb) {
		if (!this.hooks.get(name)) {
			throw new Error("Non-supported hook");
		}
		this.hooks.get(name).add(cb);
		return this;
	}

	/**
	 * Extends the App scope with property
	 * @param {name of the new propery} name
	 * @param {value of the new property} val
	 */
	add(name, val) {
		logger.info(`Adding new App scope: '${name}' to global '$' scope`);
		if (!App.prototype.$) {
			App.prototype.$ = (name) => this.plugins.get(name);
		}
		if (!App.prototype[`$${name}`]) {
			Object.defineProperty(App.prototype, `$${name}`, {
				get: () => this.plugins.get(name),
			});
		}

		this.plugins.set(name, val);
		this.logger.info(`New App scope: '${name}' was added to global '$' scope`);
		return this;
	}

	use(middleware) {
		this.handlers.add(middleware);
		return this;
	}

	/**
	 * Server starts listening on: host:port
	 */
	start() {
		logger.info("Starting App server...");
		return new Promise((resolve, reject) => {
			this.server.listen(this.port, (token) => {
				let message;
				if (!token) {
					message = `Server faild to start listening on: ${this.host}:${this.port} ❌`;
					this.logger.info(message);
					reject(new Error(message));
				} else {
					this.token = token;
					message = `Server is listening on: ${this.protocol}://${this.host}:${this.port} 🔥`;
					this.logger.info(message);
					resolve(token);
					this.shutdownHandler();
				}
			});
		});
	}

	shutdownHandler() {
		// On Windows, readline is needed to translate Ctrl-C into SIGINT
		if (process.platform === "win32") {
			const rl = readline.createInterface({
				input: process.stdin,
				output: process.stdout,
			});
			rl.on("SIGINT", () => {
				this.logger.info("SIGINT");
				process.emit("SIGINT");
			});
			rl.on("SIGTERM", () => {
				this.logger.info("SIGTERM");
				process.emit("SIGTERM");
			});
		}

		// Only intercept orderly termination signals — never catch program errors
		// (SIGABRT, SIGSEGV, SIGILL, SIGFPE, SIGBUS) because they indicate corrupt
		// process state and must be allowed to produce a core dump / non-zero exit.
		const signals = ["SIGHUP", "SIGINT", "SIGQUIT", "SIGTERM"];
		for (const sig of signals) {
			process.once(sig, async () => {
				this.logger.info(`Received ${sig} — initiating graceful shutdown…`);
				try {
					await this.stop();
				} catch (e) {
					this.logger.error("Graceful shutdown error", e);
				}
				this.logger.info("Graceful shutdown complete");
				process.exit(0);
			});
		}

		// PM2 cluster shutdown message
		process.on("message", async (msg) => {
			if (msg === "shutdown") {
				this.logger.info("PM2 shutdown message received — initiating graceful shutdown…");
				try {
					await this.stop();
				} catch (e) {
					this.logger.error("Graceful shutdown error (PM2)", e);
				}
				this.logger.info("Graceful shutdown complete");
				process.exit(0);
			}
		});
	}

	/**
	 * Server stops listening
	 */
	async stop() {
		if (this.uws) {
			this.logger.info("Server is stopping...");
			const hooks = this.hooks.get("onclose");
			try {
				await Promise.all([...hooks].map((h) => h()));
				if (this.token) {
					this.uws.us_listen_socket_close(this.token);
					this.token = null;
				}
			} catch (e) {
				this.logger.error(e);
			}
		}

		this.logger.info("Server was stopped.");
	}
}

export default App;
