#!/bin/bash
set -euo pipefail

# Production startup script for PocketBase + Litestream on Cloudflare R2
# Implements atomic restore logic, error handling, and graceful shutdown
LOG_FILE=/app/startup.log
exec > >(tee -a ${LOG_FILE}) 2>&1

# Environment validation - fail fast on missing required variables
REQUIRED_VARS=(
  "CLOUDFLARE_ACCOUNT_ID"
  "R2_BUCKET_NAME"
  "R2_ACCESS_KEY_ID"
  "R2_SECRET_ACCESS_KEY"
  "POCKETBASE_ADMIN_EMAIL"
  "POCKETBASE_ADMIN_PASSWORD"
)

for var in "${REQUIRED_VARS[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: Required environment variable $var is not set"
    exit 1
  fi
done

# Create data directory if it doesn't exist
mkdir -p ${POCKETBASE_DIR}

# Atomic restore from R2 if local database is missing or corrupted
if [[ ! -f "${DATA_PATH}" ]]; then
  echo "[$(date -Iseconds)] Local data.db not found, initiating Litestream restore from Cloudflare R2"
  
  # Execute restore with retries for transient network errors
  max_retries=5
  retry_count=0
  until litestream restore -if-replica-exists -config ${LITESTREAM_CONFIG} ${DATA_PATH}; do
    retry_count=$((retry_count+1))
    if [[ ${retry_count} -ge ${max_retries} ]]; then
      echo "ERROR: Failed to restore database after ${max_retries} attempts, exiting"
      exit 1
    fi
    echo "WARNING: Restore attempt ${retry_count} failed, retrying in 5s..."
    sleep 5
  done

  # Initialize fresh database if restore yielded no data (first run)
  if [[ ! -f "${DATA_PATH}" ]]; then
    echo "[$(date -Iseconds)] No existing replica found, initializing new PocketBase database"
    # Start PocketBase in background to initialize schema
    pocketbase serve --http=127.0.0.1:8090 --automigrate=1 &
    PB_PID=$!
    # Wait for database initialization
    sleep 10
    # Create admin user
    pocketbase admin create ${POCKETBASE_ADMIN_EMAIL} ${POCKETBASE_ADMIN_PASSWORD}
    # Shutdown temporary instance
    kill ${PB_PID}
    wait ${PB_PID} 2>/dev/null || true
  fi
else
  echo "[$(date -Iseconds)] Local data.db found, skipping restore"
fi

# Start Litestream replication in background and capture PID
echo "[$(date -Iseconds)] Starting Litestream replication to Cloudflare R2"
litestream replicate -config ${LITESTREAM_CONFIG} &
LITESTREAM_PID=$!

# Wait for Litestream to establish connection
sleep 3

# Handle graceful shutdown for SIGTERM/SIGINT
cleanup() {
  echo "[$(date -Iseconds)] Received shutdown signal, stopping services gracefully"
  kill ${LITESTREAM_PID}
  wait ${LITESTREAM_PID}
  echo "[$(date -Iseconds)] Shutdown complete"
  exit 0
}
trap cleanup SIGTERM SIGINT

# Start PocketBase server with production configuration
echo "[$(date -Iseconds)] Starting PocketBase server on 0.0.0.0:8090"
exec pocketbase serve \
  --http=0.0.0.0:8090 \
  --automigrate=1 \
  --origins="${ORIGIN:-https://axelr.in}" \
  --public-dir=/app/pb_public