import pino from "pino";

const isDevOrTest = ['development', 'test'].includes(process.env.NODE_ENV);
const resolvedLogLevel = process.env.LOG_LEVEL || (isDevOrTest ? "debug" : "info");

export const logger = pino({
	name: "adb_streaming",
	level: resolvedLogLevel,
	transport: isDevOrTest
		? { target: "pino-pretty" }
		: undefined,
});

logger.info(`Logger initialized with level: ${resolvedLogLevel}`);