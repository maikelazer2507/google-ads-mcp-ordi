#!/usr/bin/env bash
set -euo pipefail

action="${1:-plan}"
case "${action}" in
  plan|deploy-canary|promote|rollback|status) ;;
  *) echo "Usage: $0 [plan|deploy-canary|promote|rollback|status]" >&2; exit 2 ;;
esac

required=(GCP_PROJECT_ID GCP_REGION CLOUD_RUN_SERVICE)
if [[ "${action}" == "plan" || "${action}" == "deploy-canary" ]]; then
  required+=(
    RUNTIME_SERVICE_ACCOUNT SECRET_PREFIX IMAGE_DIGEST_URI CANARY_TAG MCP_BASE_URL
    DEPLOYMENT_ENVIRONMENT
    APPROVAL_BASE_URL MCP_ALLOWED_CLIENT_REDIRECT_URIS
    GOOGLE_ADS_LOGIN_CUSTOMER_ID GOOGLE_ADS_READ_CUSTOMER_IDS
    GOOGLE_ADS_ALLOWED_CUSTOMER_IDS
    GOOGLE_ADS_ALLOWED_FINAL_URL_HOSTS GOOGLE_ADS_OPERATOR_EMAILS
    GOOGLE_ADS_APPROVER_EMAILS
    DEVELOPER_TOKEN_SECRET_VERSION OAUTH_CLIENT_ID_SECRET_VERSION
    OAUTH_CLIENT_SECRET_SECRET_VERSION JWT_SIGNING_KEY_SECRET_VERSION
    STORAGE_ENCRYPTION_KEY_SECRET_VERSION APPROVAL_GOOGLE_CLIENT_ID_SECRET_VERSION
    APPROVAL_SESSION_KEY_SECRET_VERSION CHANGESET_INTEGRITY_KEY_SECRET_VERSION
  )
elif [[ "${action}" == "promote" ]]; then
  required+=(CANARY_TAG)
elif [[ "${action}" == "rollback" ]]; then
  required+=(ROLLBACK_REVISION)
fi

for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 2
  fi
done

if [[ "${action}" == "plan" || "${action}" == "deploy-canary" ]]; then
  if [[ ! "${IMAGE_DIGEST_URI}" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "IMAGE_DIGEST_URI must be an immutable @sha256 digest reference." >&2
    exit 2
  fi
  if [[ ! "${MCP_BASE_URL}" =~ ^https://[^/]+$ ]]; then
    echo "MCP_BASE_URL must be an HTTPS origin without a path or trailing slash." >&2
    exit 2
  fi
  if [[ ! "${APPROVAL_BASE_URL}" =~ ^https://[^/]+$ ]]; then
    echo "APPROVAL_BASE_URL must be an HTTPS origin without a path or trailing slash." >&2
    exit 2
  fi
  IFS=',' read -r -a redirect_list <<< "${MCP_ALLOWED_CLIENT_REDIRECT_URIS}"
  for redirect_uri in "${redirect_list[@]}"; do
    if [[ ! "${redirect_uri}" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[^?#[:space:]]*)?$ \
      || "${redirect_uri}" == *'@'* || "${redirect_uri}" == *'*'* ]]; then
      echo "MCP_ALLOWED_CLIENT_REDIRECT_URIS must contain concrete HTTPS callback URLs." >&2
      exit 2
    fi
  done
  if [[ ! "${DEPLOYMENT_ENVIRONMENT}" =~ ^[a-z][a-z0-9-]{1,31}$ ]]; then
    echo "DEPLOYMENT_ENVIRONMENT must be a stable lowercase environment name." >&2
    exit 2
  fi
  secret_versions=(
    DEVELOPER_TOKEN_SECRET_VERSION OAUTH_CLIENT_ID_SECRET_VERSION
    OAUTH_CLIENT_SECRET_SECRET_VERSION JWT_SIGNING_KEY_SECRET_VERSION
    STORAGE_ENCRYPTION_KEY_SECRET_VERSION APPROVAL_GOOGLE_CLIENT_ID_SECRET_VERSION
    APPROVAL_SESSION_KEY_SECRET_VERSION CHANGESET_INTEGRITY_KEY_SECRET_VERSION
  )
  for name in "${secret_versions[@]}"; do
    if [[ ! "${!name}" =~ ^[1-9][0-9]*$ ]]; then
      echo "${name} must be an explicit numeric Secret Manager version." >&2
      exit 2
    fi
  done
  if [[ ! "${SECRET_PREFIX}" =~ ^[a-z][a-z0-9-]{0,62}$ ]]; then
    echo "SECRET_PREFIX contains unsupported characters." >&2
    exit 2
  fi
  customer_id_values=(
    GOOGLE_ADS_LOGIN_CUSTOMER_ID GOOGLE_ADS_READ_CUSTOMER_IDS
    GOOGLE_ADS_ALLOWED_CUSTOMER_IDS
  )
  for name in "${customer_id_values[@]}"; do
    if [[ ! "${!name}" =~ ^[0-9-]+(,[0-9-]+)*$ ]]; then
      echo "${name} must contain only Google Ads customer IDs." >&2
      exit 2
    fi
  done
  email_values=(GOOGLE_ADS_OPERATOR_EMAILS GOOGLE_ADS_APPROVER_EMAILS)
  for name in "${email_values[@]}"; do
    IFS=',' read -r -a email_list <<< "${!name}"
    for email in "${email_list[@]}"; do
      if [[ ! "${email}" =~ ^[^@,[:space:]~]+@[^@,[:space:]~]+\.[^@,[:space:]~]+$ ]]; then
        echo "${name} contains an invalid email address." >&2
        exit 2
      fi
    done
  done
  delimited_values=(
    GCP_PROJECT_ID MCP_BASE_URL APPROVAL_BASE_URL DEPLOYMENT_ENVIRONMENT
    MCP_ALLOWED_CLIENT_REDIRECT_URIS
    GOOGLE_ADS_ALLOWED_FINAL_URL_HOSTS GOOGLE_ADS_OPERATOR_EMAILS
    GOOGLE_ADS_APPROVER_EMAILS
  )
  for name in "${delimited_values[@]}"; do
    if [[ "${!name}" == *'~'* || "${!name}" == *$'\n'* || "${!name}" == *$'\r'* ]]; then
      echo "${name} contains a reserved delimiter or newline." >&2
      exit 2
    fi
  done
fi

if [[ ( "${action}" == "plan" || "${action}" == "deploy-canary" || "${action}" == "promote" ) \
  && ! "${CANARY_TAG}" =~ ^[a-z][a-z0-9-]{0,62}$ ]]; then
  echo "CANARY_TAG must be a valid lowercase Cloud Run tag." >&2
  exit 2
fi

print_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

if [[ "${action}" == "status" ]]; then
  gcloud run services describe "${CLOUD_RUN_SERVICE}" \
    --project "${GCP_PROJECT_ID}" --region "${GCP_REGION}"
  gcloud run revisions list --service "${CLOUD_RUN_SERVICE}" \
    --project "${GCP_PROJECT_ID}" --region "${GCP_REGION}" --limit 10
  exit 0
fi

if [[ "${action}" == "plan" || "${action}" == "deploy-canary" ]]; then
  env_vars="^~^GOOGLE_PROJECT_ID=${GCP_PROJECT_ID}~GOOGLE_ADS_MCP_BASE_URL=${MCP_BASE_URL}~GOOGLE_ADS_MCP_APPROVAL_BASE_URL=${APPROVAL_BASE_URL}~GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS=${MCP_ALLOWED_CLIENT_REDIRECT_URIS}~GOOGLE_ADS_MCP_TRANSPORT=streamable-http~GOOGLE_ADS_MCP_PRODUCTION_MODE=true~GOOGLE_ADS_MCP_ENVIRONMENT=${DEPLOYMENT_ENVIRONMENT}~GOOGLE_ADS_MCP_STORAGE_TYPE=firestore~GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE=firestore~GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT=${GCP_PROJECT_ID}~GOOGLE_ADS_MCP_STORAGE_DISABLE_ENCRYPTION=false~GOOGLE_ADS_LOGIN_CUSTOMER_ID=${GOOGLE_ADS_LOGIN_CUSTOMER_ID}~GOOGLE_ADS_MCP_READ_CUSTOMER_IDS=${GOOGLE_ADS_READ_CUSTOMER_IDS}~GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS=${GOOGLE_ADS_ALLOWED_CUSTOMER_IDS}~GOOGLE_ADS_MCP_ALLOW_UNSCOPED_READS=false~GOOGLE_ADS_MCP_ALLOW_SENSITIVE_READS=false~GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS=${GOOGLE_ADS_ALLOWED_FINAL_URL_HOSTS}~GOOGLE_ADS_MCP_OPERATOR_EMAILS=${GOOGLE_ADS_OPERATOR_EMAILS}~GOOGLE_ADS_MCP_APPROVER_EMAILS=${GOOGLE_ADS_APPROVER_EMAILS}~GOOGLE_ADS_MCP_APPROVER_ROLES=owner~GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS=900"
  secret_vars="GOOGLE_ADS_DEVELOPER_TOKEN=${SECRET_PREFIX}-developer-token:${DEVELOPER_TOKEN_SECRET_VERSION},GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=${SECRET_PREFIX}-oauth-client-id:${OAUTH_CLIENT_ID_SECRET_VERSION},GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=${SECRET_PREFIX}-oauth-client-secret:${OAUTH_CLIENT_SECRET_SECRET_VERSION},GOOGLE_ADS_MCP_JWT_SIGNING_KEY=${SECRET_PREFIX}-jwt-signing-key:${JWT_SIGNING_KEY_SECRET_VERSION},GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY=${SECRET_PREFIX}-storage-encryption-key:${STORAGE_ENCRYPTION_KEY_SECRET_VERSION},GOOGLE_ADS_MCP_APPROVAL_GOOGLE_CLIENT_ID=${SECRET_PREFIX}-approval-google-client-id:${APPROVAL_GOOGLE_CLIENT_ID_SECRET_VERSION},GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY=${SECRET_PREFIX}-approval-session-key:${APPROVAL_SESSION_KEY_SECRET_VERSION},GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY=${SECRET_PREFIX}-changeset-integrity-key:${CHANGESET_INTEGRITY_KEY_SECRET_VERSION}"

  deploy_command=(
    gcloud run deploy "${CLOUD_RUN_SERVICE}"
    --project "${GCP_PROJECT_ID}"
    --region "${GCP_REGION}"
    --platform managed
    --image "${IMAGE_DIGEST_URI}"
    --service-account "${RUNTIME_SERVICE_ACCOUNT}"
    --execution-environment gen2
    --ingress all
    --allow-unauthenticated
    --cpu 1
    --memory 512Mi
    --concurrency 20
    --timeout 300
    --min 1
    --max 3
    --cpu-boost
    --startup-probe "httpGet.path=/readyz,httpGet.port=8080,timeoutSeconds=2,periodSeconds=5,failureThreshold=12"
    --liveness-probe "httpGet.path=/healthz,httpGet.port=8080,timeoutSeconds=2,periodSeconds=30,failureThreshold=3"
    --set-env-vars "${env_vars}"
    --set-secrets "${secret_vars}"
    --no-traffic
    --tag "${CANARY_TAG}"
    --quiet
  )

  if [[ "${action}" == "plan" ]]; then
    echo "Canary deployment plan (no command is executed):"
    print_command "${deploy_command[@]}"
    echo "The service is externally reachable because ChatGPT and Google OAuth need it; application-level Google OAuth remains mandatory."
    exit 0
  fi

  echo "Deploying an isolated revision with zero production traffic:"
  print_command "${deploy_command[@]}"
  "${deploy_command[@]}"
  echo "Canary deployed. Test its tagged URL with read-only calls before promotion."
  gcloud run services describe "${CLOUD_RUN_SERVICE}" \
    --project "${GCP_PROJECT_ID}" --region "${GCP_REGION}" \
    --format="value(status.traffic[?tag='${CANARY_TAG}'].url)"
  exit 0
fi

if [[ "${action}" == "promote" ]]; then
  promote_command=(
    gcloud run services update-traffic "${CLOUD_RUN_SERVICE}"
    --project "${GCP_PROJECT_ID}"
    --region "${GCP_REGION}"
    --to-tags "${CANARY_TAG}=100"
    --quiet
  )
  echo "Promoting the verified canary to 100 percent traffic:"
  print_command "${promote_command[@]}"
  "${promote_command[@]}"
  exit 0
fi

rollback_command=(
  gcloud run services update-traffic "${CLOUD_RUN_SERVICE}"
  --project "${GCP_PROJECT_ID}"
  --region "${GCP_REGION}"
  --to-revisions "${ROLLBACK_REVISION}=100"
  --quiet
)
echo "Rolling all traffic back to revision ${ROLLBACK_REVISION}:"
print_command "${rollback_command[@]}"
"${rollback_command[@]}"
