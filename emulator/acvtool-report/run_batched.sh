#!/bin/bash

# ==========================================
# CONFIGURATION
# ==========================================
TOTAL_CONTAINERS=5353  # Total number of report containers
BATCH_SIZE=50
PROJECT_NAME="aecs-data" # Matches your folder/project prefix

# Global variables to track the current batch for the cleanup trap
CURRENT_SERVICES=""

# ==========================================
# CTRL+C / EMERGENCY EXIT HANDLER
# ==========================================
cleanup_and_exit() {
    echo -e "\n\n🛑 Ctrl+C detected! Initiating graceful teardown..."

    if [ ! -z "$CURRENT_SERVICES" ]; then
        echo "Stopping active containers in the current batch..."
        # Stop and remove only the services that are currently running
        docker compose stop $CURRENT_SERVICES >/dev/null 2>&1
        docker compose rm -f $CURRENT_SERVICES >/dev/null 2>&1
    fi

    echo "👋 Cleanup complete. Exiting script."
    exit 1
}

# Register the trap handler for SIGINT (Ctrl+C)
trap cleanup_and_exit SIGINT

# ==========================================
# MAIN EXECUTION
# ==========================================
echo "Starting bulletproof batched Docker execution..."
echo "💡 Press Ctrl+C at any time to safely abort the run."

# Check if Docker is actually running right now
if ! docker info >/dev/null 2>&1; then
    echo "❌ Error: Docker daemon is not running. Please restart Docker Desktop first!"
    exit 1
fi

for ((i=1; i<=TOTAL_CONTAINERS; i+=BATCH_SIZE)); do
    end=$((i + BATCH_SIZE - 1))
    if [ $end -gt $TOTAL_CONTAINERS ]; then
        end=$TOTAL_CONTAINERS
    fi

    echo "------------------------------------------------"
    echo "Processing batch: report-$i to report-$end"
    echo "------------------------------------------------"

    # Reset batch strings
    compose_services=""
    container_names=""

    for ((j=i; j<=end; j++)); do
        compose_services="$compose_services report-$j"
        container_names="$container_names ${PROJECT_NAME}-report-$j-1"
    done

    # Update global tracker for the trap handler
    CURRENT_SERVICES=$compose_services

    # 1. Boot up this small batch in detached mode
    docker compose up -d $compose_services

    echo "Waiting for batch to finish..."

    # 2. Use standard 'docker wait' on the exact container names
    # This safely blocks until every container in this batch hits an exit state
    docker wait $container_names > /dev/null

    echo "Batch completed. Aggressively cleaning up container storage..."

    # 3. Wipe out the stopped container objects to reclaim memory and disk space
    docker compose rm -f $compose_services

    # Clear the tracker since this batch finished cleanly
    CURRENT_SERVICES=""

    # Quick safety buffer to let the Mac file system catch its breath
    sleep 1
done

echo "🎉 All batches finished flawlessly!"