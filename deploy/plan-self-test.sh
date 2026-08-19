#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
plan_output="$(mktemp)"
trap 'rm -f "${plan_output}"' EXIT

common_environment=(
  GCP_PROJECT_ID=example-project
  GCP_REGION=europe-west3
  CLOUD_RUN_SERVICE=google-ads-mcp
  RUNTIME_SERVICE_ACCOUNT=google-ads-mcp@example-project.iam.gserviceaccount.com
  SECRET_PREFIX=google-ads-mcp-prod
  IMAGE_DIGEST_URI=europe-west3-docker.pkg.dev/example-project/mcp/google-ads-mcp@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  CANARY_TAG=canary-test
  MCP_BASE_URL=https://mcp.example.invalid
  APPROVAL_BASE_URL=https://mcp.example.invalid
  MCP_ALLOWED_CLIENT_REDIRECT_URIS=https://chatgpt.example.invalid/oauth/callback
  DEPLOYMENT_ENVIRONMENT=production
  GOOGLE_ADS_LOGIN_CUSTOMER_ID=1111111111
  GOOGLE_ADS_READ_CUSTOMER_IDS=2222222222
  GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=2222222222
  GOOGLE_ADS_ALLOWED_FINAL_URL_HOSTS=example.invalid
  GOOGLE_ADS_OPERATOR_EMAILS=maria@example.invalid,maikel@example.invalid
  GOOGLE_ADS_APPROVER_EMAILS=maikel@example.invalid
  DEVELOPER_TOKEN_SECRET_VERSION=1
  OAUTH_CLIENT_ID_SECRET_VERSION=1
  OAUTH_CLIENT_SECRET_SECRET_VERSION=1
  JWT_SIGNING_KEY_SECRET_VERSION=1
  STORAGE_ENCRYPTION_KEY_SECRET_VERSION=1
  APPROVAL_GOOGLE_CLIENT_ID_SECRET_VERSION=1
  APPROVAL_SESSION_KEY_SECRET_VERSION=1
  CHANGESET_INTEGRITY_KEY_SECRET_VERSION=1
)

env "${common_environment[@]}" \
  bash "${repo_dir}/deploy/cloud-run.sh" plan >"${plan_output}"
grep -q 'GOOGLE_ADS_MCP_TRANSPORT=streamable-http' "${plan_output}"
grep -q 'GOOGLE_ADS_MCP_ALLOW_SENSITIVE_READS=false' "${plan_output}"
grep -q 'GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS=' "${plan_output}"
grep -q 'GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY=' "${plan_output}"

if env "${common_environment[@]}" \
  MCP_ALLOWED_CLIENT_REDIRECT_URIS=http://client.example.invalid/callback \
  bash "${repo_dir}/deploy/cloud-run.sh" plan >/dev/null 2>&1; then
  echo "Insecure redirect URI unexpectedly passed the deployment plan." >&2
  exit 1
fi

echo "Deployment plan self-test passed."
