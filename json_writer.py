import atexit
import threading
import time
from collections import defaultdict
from queue import Queue, Empty
import os
import logging
import json

def write_json_nd_output(data, output_file):
    """
    Writes the measurement data to a JSON file.

    :param data: dict - The measurement data to write.
    :param output_file: str - Path to the JSON output file.
    """
    # New async append-only writer: put the JSON object into a background queue and return immediately.
    # This avoids repeatedly reading and rewriting the full JSON file which is very slow.
    try:
        _ensure_json_writer()
        # Use a minimal payload: JSON followed by newline (NDJSON). The writer will append the line.
        line = json.dumps(data, separators=(',', ':')) + "\n"
        _json_write_queue.put((output_file, line))
    except Exception as e:
        # On any error fallback to best-effort synchronous append (still faster than rewrite)
        try:
            out_dir = os.path.dirname(output_file)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(output_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(data, indent=None, separators=(',', ':')) + "\n")
        except Exception:
            logging.debug("Error writing JSON output: %s", e)


# --- Async JSON writer implementation ---
_json_write_queue = Queue()
_json_writer_thread = None
_json_writer_thread_lock = threading.Lock()
_json_writer_stop = False
_open_file_handles = {}

# Batching configuration: write up to this many lines per file in one syscall
_JSON_BATCH_SIZE = int(os.environ.get('FMD_JSON_BATCH_SIZE', '32'))
# Fsync interval (seconds). If 0, never fsync. Otherwise fsync at least this often.
_JSON_FSYNC_INTERVAL = float(os.environ.get('FMD_JSON_FSYNC_INTERVAL', '5.0'))


def _json_writer_worker():
    """Background worker that consumes (filepath, json_line) tuples and appends them."""
    global _json_writer_stop, _open_file_handles
    # Keep a simple in-memory map of open file handles per output path to avoid open/close overhead
    try:
        # Batch lines per file to reduce syscall overhead
        batches = defaultdict(list)  # output_file -> [lines]
        last_fsync = time.time()
        while not _json_writer_stop or not _json_write_queue.empty():
            try:
                output_file, line = _json_write_queue.get(timeout=0.25)
            except Empty:
                # No item: flush batches if any pending and timeout reached
                now = time.time()
                if batches:
                    # flush all batches
                    for ofile, lines in list(batches.items()):
                        try:
                            out_dir = os.path.dirname(ofile)
                            if out_dir and not os.path.exists(out_dir):
                                try:
                                    os.makedirs(out_dir, exist_ok=True)
                                except Exception:
                                    pass

                            fh = _open_file_handles.get(ofile)
                            if fh is None:
                                fh = open(ofile, 'a', encoding='utf-8')
                                _open_file_handles[ofile] = fh

                            fh.write(''.join(lines))
                            fh.flush()
                        except Exception:
                            logging.debug('Error in async json writer while flushing batch to %s', ofile)
                        finally:
                            batches.pop(ofile, None)

                # possibly perform fsync if enough time passed
                if _JSON_FSYNC_INTERVAL > 0 and (time.time() - last_fsync) >= _JSON_FSYNC_INTERVAL:
                    for fh in list(_open_file_handles.values()):
                        try:
                            os.fsync(fh.fileno())
                        except Exception:
                            pass
                    last_fsync = time.time()

                continue

            try:
                batches[output_file].append(line)
                # flush if batch size reached
                if len(batches[output_file]) >= _JSON_BATCH_SIZE:
                    lines = batches.pop(output_file)
                    try:
                        out_dir = os.path.dirname(output_file)
                        if out_dir and not os.path.exists(out_dir):
                            try:
                                os.makedirs(out_dir, exist_ok=True)
                            except Exception:
                                pass

                        fh = _open_file_handles.get(output_file)
                        if fh is None:
                            fh = open(output_file, 'a', encoding='utf-8')
                            _open_file_handles[output_file] = fh

                        fh.write(''.join(lines))
                        fh.flush()
                    except Exception:
                        logging.debug('Error in async json writer while writing batch to %s', output_file)
            except Exception:
                logging.debug('Error in async json writer while processing queue')
            finally:
                try:
                    _json_write_queue.task_done()
                except Exception:
                    pass
    finally:
        # Close open file handles on exit
        for fh in _open_file_handles.values():
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        _open_file_handles = {}


def _ensure_json_writer():
    """Start the background writer thread lazily."""
    global _json_writer_thread
    if _json_writer_thread and _json_writer_thread.is_alive():
        return
    with _json_writer_thread_lock:
        if _json_writer_thread and _json_writer_thread.is_alive():
            return
        _json_writer_thread = threading.Thread(target=_json_writer_worker, name='fmd-json-writer', daemon=True)
        _json_writer_thread.start()


def _shutdown_json_writer():
    """Signal the writer to stop and wait briefly for queue to drain."""
    global _json_writer_stop, _json_writer_thread
    _json_writer_stop = True
    try:
        # Wait a short time for the background thread to finish queued work
        if _json_write_queue is not None:
            _json_write_queue.join()
    except Exception:
        pass
    try:
        if _json_writer_thread:
            _json_writer_thread.join(timeout=1.0)
    except Exception:
        pass


# Register shutdown to flush queued entries at process exit
atexit.register(_shutdown_json_writer)


def write_json_output(result, output_file):
    """
    Writes the build result to a JSON file.

    :param result: dict - The result to write to the JSON file.
    :param output_file: str - Path to the JSON output file.
    """

    # Append the result to the JSON file
    try:
        with open(output_file, "r") as file:
            data = json.load(file)
    except FileNotFoundError:
        data = []
    except Exception as err:
        logging.error(f"Error writing to file: {err}")
        data = []

    data.append(result)
    try:
        with open(output_file, "w") as file:
            json.dump(data, file, indent=4)
            file.write("\n")  # Add a newline
    except Exception as err:
        logging.error(f"Error writing to file: {err}")


def write_text_output(result, output_file):
    """
    Writes the build result to a text file.

    :param result: str - The result to write to the text file.
    :param output_file: str - Path to the text output file.
    """
    try:
        with open(output_file, "a") as file:
            file.write("\"" + result + "\",")
    except Exception as err:
        logging.error(f"Error writing to file: {err}")