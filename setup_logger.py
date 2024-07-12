import logging
import sys
import os

# Global flag to indicate whether the logger has been set up
logger_configured = False


def setup_logger(loglevel='INFO'):
    global logger_configured
    if logger_configured:
        return
    debug = os.environ.get('FMD_DEBUG', False)
    root = logging.getLogger()
    # Remove all existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Setup new handler
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(thread)d - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    if not debug:
        root.setLevel(logging.INFO)
        handler.setLevel(logging.INFO)
    else:
        logging.basicConfig(level=logging.DEBUG)
        handler.setLevel(logging.DEBUG)
    root.addHandler(handler)
    logger_configured = True