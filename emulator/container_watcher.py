import sys
import os
import time
import argparse
import docker
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class ExperimentHandler(FileSystemEventHandler):
    def __init__(self):
        self.client = docker.from_env()

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
            print(f"[*] Detected {filename} in folder: {image_name}")
            self.stop_container_by_image(image_name)

    def stop_container_by_image(self, target_name):
        try:
            containers = self.client.containers.list()
            found = False
            for container in containers:
                image_tag = container.attrs['Config']['Image']

                # Check if our folder name is part of the container's image name
                if target_name in image_tag:
                    print(f"[!] Matching container found: {container.name} ({container.short_id})")
                    container.stop()
                    print(f"[✓] Successfully stopped {container.name}")
                    found = True

            if not found:
                print(f"[?] No running container found for image: {target_name}")

        except Exception as e:
            print(f"[X] Error interacting with Docker: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Watch a directory for experiment_summary.json and stop Docker containers.")
    parser.add_argument("path", help="The root directory to monitor")
    args = parser.parse_args()

    if not os.path.isdir(args.path):
        print(f"Error: {args.path} is not a valid directory.")
        sys.exit(1)

    event_handler = ExperimentHandler()
    observer = Observer()
    observer.schedule(event_handler, args.path, recursive=True)

    print(f"--- Monitoring started on: {os.path.abspath(args.path)} ---")
    print("Press Ctrl+C to stop.")

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\nStopping monitor...")
    observer.join()


if __name__ == "__main__":
    main()