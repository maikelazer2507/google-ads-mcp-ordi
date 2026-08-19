#!/usr/bin/env bash
set -euo pipefail

mode="${1:-plan}"
if [[ "${mode}" != "plan" && "${mode}" != "apply" ]]; then
  echo "Usage: $0 [plan|apply]" >&2
  exit 2
fi

required=(GCP_PROJECT_ID GCP_REGION ARTIFACT_REPOSITORY RELEASE_ID PYTHON_IMAGE UV_IMAGE)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 2
  fi
done

digest_pattern='@sha256:[0-9a-f]{64}$'
for image in "${PYTHON_IMAGE}" "${UV_IMAGE}"; do
  if [[ ! "${image}" =~ ${digest_pattern} ]]; then
    echo "Release base images must be pinned by sha256 digest: ${image}" >&2
    exit 2
  fi
done

if [[ ! "${RELEASE_ID}" =~ ^[0-9A-Za-z][0-9A-Za-z._-]{0,62}$ ]]; then
  echo "RELEASE_ID contains unsupported characters." >&2
  exit 2
fi

image_tag_uri="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${ARTIFACT_REPOSITORY}/google-ads-mcp:${RELEASE_ID}"
build_command=(
  docker buildx build
  --pull
  --platform linux/amd64
  --build-arg "PYTHON_IMAGE=${PYTHON_IMAGE}"
  --build-arg "UV_IMAGE=${UV_IMAGE}"
  --provenance mode=max
  --sbom true
  --tag "${image_tag_uri}"
  --push
  .
)

printf 'Release image: %s\n' "${image_tag_uri}"
printf '  '
printf '%q ' "${build_command[@]}"
printf '\n'

if [[ "${mode}" == "plan" ]]; then
  echo "Plan only. Authenticate Docker to Artifact Registry, then re-run with 'apply'."
  exit 0
fi

command -v docker >/dev/null || { echo 'docker is required.' >&2; exit 2; }
docker buildx version >/dev/null
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing a production build from a dirty working tree." >&2
  exit 2
fi
"${build_command[@]}"

echo "Resolve the immutable result and export it before deployment:"
echo "  gcloud artifacts docker images describe '${image_tag_uri}' --project '${GCP_PROJECT_ID}'"
echo "  export IMAGE_DIGEST_URI='${image_tag_uri%:*}@sha256:<digest-from-command>'"
