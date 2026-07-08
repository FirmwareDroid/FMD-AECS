#!/bin/bash

# Find all docker-compose-*.yml files and sort them numerically (-V)
files=$(ls docker-compose-*.yml | sort -V)

for file in $files; do
    echo "========================================"
    echo "Starting $file..."
    echo "========================================"

    # Run the compose file in detached mode
    docker compose -f "$file" up

    # Check if the command succeeded
    if [ $? -ne 0 ]; then
        echo "Error: Failed to start $file. Exiting."
        exit 1
    fi
done

echo "All services have been started successfully!"