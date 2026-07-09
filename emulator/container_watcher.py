import sys
import os
import time
import argparse
import logging
import docker
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class ExperimentHandler(FileSystemEventHandler):
    def __init__(self):
        try:
            self.client = docker.from_env()
        except Exception:
            logging.exception('Failed to initialize Docker client')
            self.client = None

    def on_created(self, event):
        self._process(event)

    def on_moved(self, event):
        # Handles cases where a file is moved into the watched directory
        self._process(event, is_move=True)

    def _process(self, event, is_move=False):
        # We only care about files
        if event.is_directory:
            return

        # Determine the path based on event type
        path = event.dest_path if is_move else event.src_path
        filename = os.path.basename(path)

        if filename == "experiment_summary.json":
            # Extract the parent directory name
            # Example: /path/to/IMAGE_NAME/experiment_summary.json -> IMAGE_NAME
            image_name = os.path.basename(os.path.dirname(path))
            logging.info('Detected %s in folder: %s', filename, image_name)
            try:
                self.stop_container_by_image(image_name)
            except Exception:
                logging.exception('Error while attempting to stop container for image: %s', image_name)

    def stop_container_by_image(self, target_name):
        try:
            if not self.client:
                try:
                    self.client = docker.from_env()
                except Exception:
                    logging.exception('Docker client unavailable and could not be reinitialized')
                    return

            containers = self.client.containers.list()
            found = False
            for container in containers:
                image_tag = container.attrs.get('Config', {}).get('Image', '')

                # Check if our folder name is part of the container's image name
                if target_name in image_tag:
                    logging.info('Matching container found: %s (%s)', container.name, container.short_id)
                    try:
                        container.stop()
                        logging.info('Successfully stopped %s', container.name)
                    except Exception:
                        logging.exception('Failed to stop container %s', container.name)
                    found = True

            if not found:
                logging.debug('No running container found for image: %s', target_name)

        except Exception:
            logging.exception('Error interacting with Docker while stopping container for: %s', target_name)


def main():
    parser = argparse.ArgumentParser(
        description="Watch a directory for experiment_summary.json and stop Docker containers.")
    parser.add_argument("path", help="The root directory to monitor")
    args = parser.parse_args()

    if not os.path.isdir(args.path):
        logging.error('%s is not a valid directory.', args.path)
        sys.exit(1)

    # Setup logging for this watcher
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    event_handler = ExperimentHandler()
    observer = Observer()
    observer.schedule(event_handler, args.path, recursive=True)

    # On startup, scan for any already-existing experiment_summary.json files and stop matching containers
    logging.info('Scanning %s for existing experiment_summary.json files...', args.path)
    for root, dirs, files in os.walk(args.path):
        for fname in files:
            if fname == 'experiment_summary.json':
                full = os.path.join(root, fname)
                image_name = os.path.basename(os.path.dirname(full))
                logging.info('Found existing %s in folder: %s (path=%s) -> stopping containers for image: %s', fname, image_name, full, image_name)
                try:
                    event_handler.stop_container_by_image(image_name)
                except Exception:
                    logging.exception('Error while stopping container for existing file: %s', full)

    logging.info('--- Monitoring started on: %s ---', os.path.abspath(args.path))
    observer.start()
    observer.stop()
    print("\nStopping monitor...")
    observer.join()


if __name__ == "__main__":
    main()