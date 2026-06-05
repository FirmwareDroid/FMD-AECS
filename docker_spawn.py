import docker
import time
import threading
import os
from queue import Queue

# --- CONFIGURATION MATCHING YOUR COMPOSER SETUP ---
IMAGE_PATTERN = "sdk_phone64_arm64"
MAX_CONCURRENT_CONTAINERS = 30
SCAN_INTERVAL_SECONDS = 10

# Base paths for your data volumes
BASE_EMULATOR_DIR = os.path.abspath("./emulator/emulator_out")
ENV_FILE_PATH = os.path.abspath("./env/.env")

# Network Names (Must match the explicit network names in Step 1)
FRONTEND_NET = "project_frontend"
ANDROID_NET = "project_android"

client = docker.from_env()
image_queue = Queue()
MANAGED_LABEL = {"managed_by": "android_scheduler"}


def get_available_resources(active_containers):
    """
    Scans currently active containers to find an available internal IP
    and a safe block of host ports to assign to the next emulator.
    """
    used_ips = set()
    used_ports = set()

    for c in active_containers:
        try:
            # FIX 2: Refresh container attributes to get real-time network states
            c.reload()

            # Check IP allocations
            networks_metadata = c.attrs.get('NetworkSettings', {}).get('Networks', {})
            if ANDROID_NET in networks_metadata:
                ip = networks_metadata[ANDROID_NET].get('IPAddress')
                if ip: used_ips.add(ip)

            # Check host port allocations
            port_bindings = c.attrs.get('NetworkSettings', {}).get('Ports', {}) or {}
            for port_list in port_bindings.values():
                if port_list:
                    for binding in port_list:
                        used_ports.add(int(binding.get('HostPort', 0)))
        except Exception:
            continue  # Skip if a container finishes and disappears mid-check

    # FIX 1: Start dynamic slots at 50 to match your preference
    for slot in range(50, 50 + MAX_CONCURRENT_CONTAINERS):
        target_ip = f"172.31.250.{slot}"

        # Exact port progression math based on the slot number
        p_8554 = 8554 + slot
        p_5555 = 5555 + slot
        p_2222 = 2222 + slot
        p_5000 = 5000 + slot

        target_ports = {p_8554, p_5555, p_2222, p_5000}

        if target_ip not in used_ips and not target_ports.intersection(used_ports):
            port_mapping = {
                "8554/tcp": p_8554,
                "5555/tcp": p_5555,
                "22/tcp": p_2222,
                "5037/tcp": p_5000
            }
            return slot, target_ip, port_mapping
    return None, None, None


def image_scanner():
    print(f"[Scanner] Watching for new Android images matching '{IMAGE_PATTERN}'...")
    while True:
        try:
            for image in client.images.list():
                for tag in image.tags:
                    if IMAGE_PATTERN in tag and "auto_queued" not in image.labels:
                        print(f"[Scanner] Found new Android build: {tag}. Queueing.")
                        image_queue.put(tag)

                        image.tag(repository=tag.split(':')[0], tag=tag.split(':')[1],
                                  labels={"auto_queued": "true"})
        except Exception as e:
            print(f"[Scanner Error] {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)


def parse_env_file(filepath):
    """Parses a standard .env file into a dictionary for the Docker SDK."""
    env_dict = {}
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    env_dict[key.strip()] = value.strip()
    return env_dict


def container_runner():
    print(f"[Runner] Scheduler active. Waiting for images...")
    while True:
        try:
            active = client.containers.list(filters={"label": "managed_by=android_scheduler"})
            current_count = len(active)

            if not image_queue.empty() and current_count < MAX_CONCURRENT_CONTAINERS:
                slot_id, target_ip, port_mapping = get_available_resources(active)

                if slot_id is None:
                    print("[Runner] Resource calculation error. Waiting for an execution slot to clear.")
                    time.sleep(5)
                    continue

                next_image = image_queue.get()
                clean_img_name = next_image.split(':')[0].split('/')[-1]

                container_name = f"android_emulator_{slot_id}"

                out_dir = f"{BASE_EMULATOR_DIR}/{clean_img_name}"
                os.makedirs(f"{out_dir}/app_testing_tools/out", exist_ok=True)

                print(f"[Scheduler] Slot {slot_id} allocated. Booting {container_name} on IP {target_ip}...")
                parsed_environment = parse_env_file(ENV_FILE_PATH)

                container = client.containers.run(
                    next_image,
                    name=container_name,
                    detach=True,
                    platform="linux/arm64",
                    restart_policy={"Name": "unless-stopped"},
                    environment=parsed_environment,
                    ports=port_mapping,
                    dns=["172.31.250.2"],
                    sysctls={
                        "net.ipv4.ip_nonlocal_bind": "1",
                        "net.ipv6.conf.all.disable_ipv6": "1"
                    },
                    devices=["/dev/kvm:/dev/kvm:rwm"],
                    labels=MANAGED_LABEL,
                    volumes={
                        f"{out_dir}": {"bind": "/android/testing_service/out", "mode": "rw"},
                        f"{out_dir}/app_testing_tools/out": {"bind": "/android/testing_service/app_testing_tools/out",
                                                             "mode": "rw"}
                    },
                    network=FRONTEND_NET
                )

                android_network_obj = client.networks.get(ANDROID_NET)
                android_network_obj.connect(container, ipv4_address=target_ip)

                image_queue.task_done()

            time.sleep(4)
        except Exception as e:
            print(f"[Runner Error] {e}")
            time.sleep(5)


if __name__ == "__main__":
    scanner_thread = threading.Thread(target=image_scanner, daemon=True)
    scanner_thread.start()

    try:
        container_runner()
    except KeyboardInterrupt:
        print("\nShutting down scheduler engine gracefully...")