#!/bin/sh

set -eu

VAULT_KV_MOUNT="${VAULT_KV_MOUNT:-secret}"
VAULT_DB_SECRET_PATH="${VAULT_DB_SECRET_PATH:-database/postgres}"
VAULT_KAFKA_SECRET_PATH="${VAULT_KAFKA_SECRET_PATH:-kafka/config}"

if [ -z "${VAULT_ADDR:-}" ]; then
  echo "VAULT_ADDR is not set"
  exit 1
fi

if [ -z "${VAULT_TOKEN:-}" ]; then
  echo "VAULT_TOKEN is not set"
  exit 1
fi

if [ -z "${POSTGRES_HOST:-}" ]; then
  echo "POSTGRES_HOST is not set"
  exit 1
fi

if [ -z "${POSTGRES_PORT:-}" ]; then
  echo "POSTGRES_PORT is not set"
  exit 1
fi

if [ -z "${POSTGRES_DB:-}" ]; then
  echo "POSTGRES_DB is not set"
  exit 1
fi

if [ -z "${POSTGRES_USER:-}" ]; then
  echo "POSTGRES_USER is not set"
  exit 1
fi

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
  echo "POSTGRES_PASSWORD is not set"
  exit 1
fi

if [ -z "${KAFKA_BOOTSTRAP_SERVERS:-}" ]; then
  echo "KAFKA_BOOTSTRAP_SERVERS is not set"
  exit 1
fi

if [ -z "${KAFKA_PREDICTION_TOPIC:-}" ]; then
  echo "KAFKA_PREDICTION_TOPIC is not set"
  exit 1
fi

if [ -z "${KAFKA_CONSUMER_GROUP:-}" ]; then
  echo "KAFKA_CONSUMER_GROUP is not set"
  exit 1
fi

export VAULT_ADDR
export VAULT_TOKEN

echo "Waiting for Vault..."

for i in $(seq 1 30); do
  if vault status >/dev/null 2>&1; then
    echo "Vault is available"
    break
  fi

  echo "Vault is not ready yet. Waiting..."
  sleep 2
done

vault status >/dev/null 2>&1

echo "Writing PostgreSQL secrets to Vault..."

vault kv put "${VAULT_KV_MOUNT}/${VAULT_DB_SECRET_PATH}" \
  POSTGRES_HOST="${POSTGRES_HOST}" \
  POSTGRES_PORT="${POSTGRES_PORT}" \
  POSTGRES_DB="${POSTGRES_DB}" \
  POSTGRES_USER="${POSTGRES_USER}" \
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD}"

echo "PostgreSQL secrets were written to Vault"

echo "Writing Kafka settings to Vault..."

vault kv put "${VAULT_KV_MOUNT}/${VAULT_KAFKA_SECRET_PATH}" \
  KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS}" \
  KAFKA_PREDICTION_TOPIC="${KAFKA_PREDICTION_TOPIC}" \
  KAFKA_CONSUMER_GROUP="${KAFKA_CONSUMER_GROUP}"

echo "Kafka settings were written to Vault"