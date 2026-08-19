# Production deployment

These scripts create a reviewable, reversible Cloud Run release. They never
contain secret values and default to a no-op plan.

## Safety model

- `bootstrap-gcp.sh` defaults to `plan`; only `apply` creates infrastructure.
- `build-release.sh` defaults to `plan`; only `apply` pushes an image.
- `cloud-run.sh` defaults to `plan`; `deploy-canary` creates a zero-traffic
  revision, `promote` moves traffic, and `rollback` restores a named revision.
- Release images and both build-stage images must be immutable digest
  references. Python packages are resolved from the hash-bearing `uv.lock`.
- Runtime secrets come from individually authorized Secret Manager resources
  replicated only in the configured EU region and referenced at explicit
  version numbers. Secret values are never CLI environment flags.
- The Cloud Run runtime identity has only `roles/datastore.user` plus
  `roles/secretmanager.secretAccessor` on the eight application secrets.

Cloud Run must allow unauthenticated network access so ChatGPT and Google's
OAuth redirects can reach the application. This does **not** bypass the
application's Google OAuth authentication.

The production configuration contract is explicit. Non-secret controls are
`GOOGLE_ADS_MCP_PRODUCTION_MODE=true`,
`GOOGLE_ADS_MCP_TRANSPORT=streamable-http`,
`GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE=firestore`,
`GOOGLE_ADS_MCP_ENVIRONMENT=production`,
`GOOGLE_ADS_MCP_APPROVAL_BASE_URL`,
`GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS`,
`GOOGLE_ADS_MCP_OPERATOR_EMAILS`,
`GOOGLE_ADS_MCP_APPROVER_EMAILS`,
`GOOGLE_ADS_MCP_READ_CUSTOMER_IDS`, and
`GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS`. The developer token, MCP OAuth client
credentials, JWT signing key, storage-encryption key,
`GOOGLE_ADS_MCP_APPROVAL_GOOGLE_CLIENT_ID`, and
`GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY`, and the independent
`GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY` are Secret Manager references. Do not
reuse either key or introduce legacy aliases for the change-set storage
variable.

## One-time setup

1. Copy `deploy/env.example` to a location outside the repository, fill in the
   identifiers, and source it.
2. Review `bash deploy/bootstrap-gcp.sh plan`.
3. Run `bash deploy/bootstrap-gcp.sh apply` with an administrator identity.
   A newly created Firestore database has PITR and deletion protection. If a
   default database already exists, verify those settings separately before
   production promotion.
4. Add each secret value interactively so it does not enter shell history:

   ```shell
   gcloud secrets versions add google-ads-mcp-prod-developer-token \
     --project "$GCP_PROJECT_ID" --data-file=-
   ```

   Repeat for `oauth-client-id`, `oauth-client-secret`, `jwt-signing-key`,
   `storage-encryption-key`, `approval-google-client-id`,
   `approval-session-key`, and `changeset-integrity-key`. Record the returned
   numeric versions in the external environment file. Signing, session,
   integrity, and encryption keys must be independently generated high-entropy
   values of at least 32 characters.
5. Set the Google OAuth client's authorized redirect URI to the callback shown
   by FastMCP for the exact `MCP_BASE_URL`, and put the exact ChatGPT client
   callback URI in `MCP_ALLOWED_CLIENT_REDIRECT_URIS`. Do not use wildcard
   hosts. Give Maria and Maikel separate
   Google/ChatGPT identities; both belong in the operator allowlist, while only
   Maikel belongs in the approver allowlist. Sofia belongs in neither list.

## Every release

1. Require green CI, completed review, and a clean, immutable commit.
2. Resolve and independently verify the publisher digests for the pinned
   Python and uv tags. Put the digest references in the external env file.
3. Authenticate Docker to Artifact Registry, then review
   `bash deploy/build-release.sh plan` and run
   `bash deploy/build-release.sh apply`.
4. Resolve the pushed image digest and set `IMAGE_DIGEST_URI` to the
   `@sha256:...` reference, not the tag.
5. Deploy that exact digest first to a separate staging project/service and test
   end-to-end OAuth, approval, mutation, verification and rollback only against
   a Google Ads test account. Use a separate external env file with
   `DEPLOYMENT_ENVIRONMENT=staging` and staging-only secrets/allowlists.
6. Review `bash deploy/cloud-run.sh plan`, then run
   `bash deploy/cloud-run.sh deploy-canary`.
7. Test the tagged canary URL according to `docs/PRODUCTION_RUNBOOK.md`.
8. Run `bash deploy/cloud-run.sh promote` only after the release gate is signed
   off. If checks fail, leave production traffic untouched.

The scripts do not delete revisions, service accounts, secrets, databases, or
repositories. Keep at least the last two known-good image digests and revision
names for rollback.
