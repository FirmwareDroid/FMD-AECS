import os
import time
import logging
import subprocess
import threading
import docker
from dotenv import dotenv_values
import job_queue
from queue import Queue

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

client = docker.from_env()

# --- Resource Pool Configuration ---
MAX_CONCURRENT_EMULATORS = 20

# Generate pools for IPs and Ports
ip_pool = Queue()
for i in range(130, 130 + MAX_CONCURRENT_EMULATORS):
    ip_pool.put(f"172.31.250.{i}")

port_pool = Queue()
for i in range(MAX_CONCURRENT_EMULATORS):
    port_pool.put({
        'grpc': 8554 + i,
        'adb': 5555 + i,
        'ssh': 2222 + i,
        'scrcpy': 5000 + i
    })


def process_emulator_job(job, static_ip, ports):
    """Handles the full lifecycle of a single emulator container."""
    image_name = job['image_name']
    container_name = f"android_emulator_{job['id']}"
    env_vars = dotenv_values("./env/.env")
    container = None

    logging.info(f"[Job {job['id']}] Starting container {container_name} with IP {static_ip}")

    # 1. Resolve absolute paths for the Docker daemon
    host_base_dir = os.path.abspath(f"./emulator/emulator_out/{image_name}")
    host_tools_dir = os.path.abspath(f"./emulator/emulator_out/{image_name}/app_testing_tools/out")

    # Ensure the directories exist on the host before mounting
    os.makedirs(host_base_dir, exist_ok=True)
    os.makedirs(host_tools_dir, exist_ok=True)

    try:
        container = client.containers.create(
            image=image_name,
            name=container_name,
            platform="linux/arm64" if "arm64" in image_name else "linux/amd64",
            environment=env_vars,
            ports={
                '8554/tcp': ports['grpc'],
                '5555/tcp': ports['adb'],
                '22/tcp': ports['ssh'],
                '5037/tcp': ports['scrcpy']
            },
            sysctls={"net.ipv6.conf.all.disable_ipv6": 1},
            dns=["172.31.250.2"],
            volumes={
                host_base_dir: {
                    'bind': '/android/testing_service/out',
                    'mode': 'rw'
                },
                host_tools_dir: {
                    'bind': '/android/testing_service/app_testing_tools/out',
                    'mode': 'rw'
                }
            },
            devices=["/dev/kvm:/dev/kvm:rwm"],
            network="project_frontend",
            detach=True
        )

        # 2. Attach static IP to the android network
        android_network = client.networks.get("project_android")
        android_network.connect(container, ipv4_address=static_ip)

        # 3. Start Container
        container.start()
        logging.info(f"[Job {job['id']}] Container {container_name} is running.")

        # 4. Trigger Testing Script
        time.sleep(10)

        logging.info(f"[Job {job['id']}] Executing testing tool on {container_name}...")
        cmd = [
            "python3", "./emulator/run_app_testing_tool.py",
            "--filter", container_name,
            "--mode", "pipeline"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logging.error(f"[Job {job['id']}] Test script finished with error: {result.stderr}")
        else:
            logging.info(f"[Job {job['id']}] Test script completed successfully.")

    except Exception as e:
        logging.error(f"[Job {job['id']}] Pipeline error: {e}")
    finally:
        # 5. Monitor and Teardown
        if container:
            try:
                # Refresh container status
                container.reload()

                # Block the thread until the container stops natively
                if container.status != 'exited':
                    logging.info(
                        f"[Job {job['id']}] Monitoring container {container_name}. Waiting for it to stop natively...")
                    container.wait()
                    logging.info(f"[Job {job['id']}] Container {container_name} has stopped natively.")

                # Remove the now-stopped container
                logging.info(f"[Job {job['id']}] Removing stopped container {container_name}")
                container.remove(v=True)  # v=True removes associated anonymous volumes

                # Remove the image to free space
                logging.info(f"[Job {job['id']}] Removing image {image_name} to free disk space")
                client.images.remove(image_name, force=True)

            except docker.errors.APIError as e:
                logging.warning(f"[Job {job['id']}] Cleanup error during teardown: {e}")

        # Mark job as completed in DB
        job_queue.mark_job_completed(job['id'])

        # Return leased resources to the pool
        ip_pool.put(static_ip)
        port_pool.put(ports)
        logging.info(f"[Job {job['id']}] Resources released.")


def daemon_loop():
    job_queue.init_db()
    logging.info("Consumer daemon started. Waiting for jobs...")

    while True:
        # Check if we have resources available to process a new job
        if not ip_pool.empty() and not port_pool.empty():
            job = job_queue.fetch_next_job()

            if job:
                # Checkout resources
                leased_ip = ip_pool.get()
                leased_ports = port_pool.get()

                # Spawn worker thread
                worker = threading.Thread(
                    target=process_emulator_job,
                    args=(job, leased_ip, leased_ports),
                    daemon=True
                )
                worker.start()
            else:
                # No pending jobs, sleep before polling again
                time.sleep(5)
        else:
            # Max concurrency reached, wait for a worker to release resources
            time.sleep(5)


if __name__ == "__main__":
    daemon_loop()