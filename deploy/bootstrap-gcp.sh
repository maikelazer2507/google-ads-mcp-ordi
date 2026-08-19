#!/usr/bin/env bash
set -euo pipefail

mode="${1:-plan}"
if [[ "${mode}" != "plan" && "${mode}" != "apply" ]]; then
  echo "Usage: $0 [plan|apply]" >&2
  exit 2
fi

required=(GCP_PROJECT_ID GCP_REGION ARTIFACT_REPOSITORY RUNTIME_SERVICE_ACCOUNT SECRET_PREFIX)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 2
  fi
done

service_account_name="${RUNTIME_SERVICE_ACCOUNT%@*}"
if [[ "${RUNTIME_SERVICE_ACCOUNT}" != "${service_account_name}@${GCP_PROJECT_ID}.iam.gserviceaccount.com" ]]; then
  echo "RUNTIME_SERVICE_ACCOUNT must belong to GCP_PROJECT_ID." >&2
  exit 2
fi

print_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

run() {
  if [[ "${mode}" == "plan" ]]; then
    print_command "$@"
  else
    "$@"
  fi
}

echo "Mode: ${mode}"
echo "Project: ${GCP_PROJECT_ID}"
echo "Region: ${GCP_REGION}"

run gcloud services enable \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  googleads.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  --project "${GCP_PROJECT_ID}"

if [[ "${mode}" == "plan" ]] || ! gcloud iam service-accounts describe \
  "${RUNTIME_SERVICE_ACCOUNT}" --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  run gcloud iam service-accounts create "${service_account_name}" \
    --display-name "Google Ads MCP runtime" \
    --project "${GCP_PROJECT_ID}"
fi

if [[ "${mode}" == "plan" ]] || ! gcloud artifacts repositories describe \
  "${ARTIFACT_REPOSITORY}" --location "${GCP_REGION}" \
  --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  run gcloud artifacts repositories create "${ARTIFACT_REPOSITORY}" \
    --repository-format docker \
    --location "${GCP_REGION}" \
    --description "Production MCP images" \
    --immutable-tags \
    --project "${GCP_PROJECT_ID}"
fi

if [[ "${mode}" == "plan" ]] || ! gcloud firestore databases describe \
  --database "(default)" --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  run gcloud firestore databases create \
    --database "(default)" \
    --location "${GCP_REGION}" \
    --type firestore-native \
    --delete-protection \
    --enable-pitr \
    --project "${GCP_PROJECT_ID}"
fi

# Firestore contains OAuth state and the durable approval/audit records. This
# is the only project-level data role granted to the runtime identity.
run gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
  --member "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role roles/datastore.user \
  --condition None

secret_suffixes=(
  developer-token
  oauth-client-id
  oauth-client-secret
  jwt-signing-key
  storage-encryption-key
  approval-google-client-id
  approval-session-key
  changeset-integrity-key
)

for suffix in "${secret_suffixes[@]}"; do
  secret_name="${SECRET_PREFIX}-${suffix}"
  if [[ "${mode}" == "plan" ]] || ! gcloud secrets describe "${secret_name}" \
    --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    run gcloud secrets create "${secret_name}" \
      --replication-policy user-managed \
      --locations "${GCP_REGION}" \
      --project "${GCP_PROJECT_ID}"
  fi
  run gcloud secrets add-iam-policy-binding "${secret_name}" \
    --member "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --role roles/secretmanager.secretAccessor \
    --project "${GCP_PROJECT_ID}"
done

if [[ "${mode}" == "plan" ]]; then
  echo "Plan only. Re-run with 'apply' after reviewing every command."
else
  echo "Bootstrap complete. Secret containers exist, but no secret values were created."
fi
