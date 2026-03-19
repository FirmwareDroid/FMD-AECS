#!/bin/bash
echo "Starting monkey experiment in all running android_emulator containers..."
for name in $(docker ps --format '{{.Names}}' | grep "android_emulator"); do
    echo "Running monkey experiment in $name..."
    docker exec -d "$name" python3 /android/testing_service/run_experiment.py --mode monkey --test-only-one --pcap-http-port 54320 --socks5-address 172.31.250.4
done

