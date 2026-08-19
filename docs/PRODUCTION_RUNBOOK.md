# Google Ads MCP production runbook

## Production gate

Live writes remain disabled until every item below is evidenced in the release
record. Urgency is not an exception to this gate.

- CI is green for Python 3.10-3.13, deterministic smoke tests, dependency
  audit, Bandit, credential pattern scan, non-root image verification, and
  Trivy configuration/image scans.
- The protected, manually triggered Google Ads **test-account** integration
  job is green. It must never point at the production customer.
- Security-sensitive code and release configuration have independent review.
  This is a release-quality control, not a runtime four-eyes guarantee.
- A real human approval is performed outside the model-callable tool surface,
  bound to Maikel's authenticated identity, exact payload hash, account,
  environment and expiry. Maria can propose but cannot approve.
- Change sets are durable and atomically transition
  `PENDING -> APPROVED -> CONSUMED` (or `EXPIRED`), with one-time
  execution under concurrency.
- Application audit records and Google Ads change events together provide
  actor/approver, request/change-set references, outcome, and timing evidence.
  This is operational evidence, not independently tamper-evident proof until
  the external logging gate below is implemented.
- Read and write operations are both restricted to the practice customer.
  Sensitive patient, health, call transcript, CRM free text, or audience data
  cannot enter prompts, logs or Google Ads payloads.
- Drift, stale reads and ambiguous post-mutation verification fail closed.
  A change with an uncertain outcome is reconciled before any retry.
- Destructive/open-world MCP annotations accurately describe every mutating
  tool. No delete, access-management, customer upload, billing, bidding,
  conversion-goal or bulk mutation is enabled in the first release.
- Rollback was executed successfully against the Google Ads test account and
  Cloud Run can be returned to the previous known-good revision.

## Staging and production canary verification

First deploy the exact production image digest to a dedicated, separate
staging Google Cloud project and Cloud Run service configured with
`DEPLOYMENT_ENVIRONMENT=staging`, its own stable base URL/OAuth
redirects/secrets/Firestore, and only a Google Ads test account. Never put
the production customer in the staging allowlists. Use the operator's and
owner-approver's separate browser/ChatGPT sessions and record evidence for:

1. `/healthz` answers only process liveness and `/readyz` fails closed when a
   required production control is absent. An unauthenticated MCP request still
   receives an OAuth challenge and cannot list Ads data.
2. The operator identity can authenticate, list only the test account, run a
   bounded read query, and create a proposal. It cannot approve or bypass an
   expired proposal.
3. Maikel as owner-approver sees the exact account, object IDs and names,
   before/after values, expected effect, risk, observation window and rollback
   before approving.
4. Copying text from the model or replaying/concurrently submitting a token
   cannot create approval or execute twice.
5. A mutation and its rollback succeed on the dedicated Google Ads test
   account.
6. Wrong-account IDs, non-keyword criterion IDs, shared budgets, unapproved
   landing-page hosts, more-than-10-percent budget increases, and live-state
   drift are rejected.
7. Cloud Logging contains structured request/change IDs but no developer
   token, OAuth token, secret, full sensitive query, prompt, patient data or
   authorization header.
8. At least two instances can use the same OAuth/change-set state in Firestore
   without replay, refresh, encryption or race failures.
9. Firestore Emulator tests cover transactions, contention, replay, expiry and
   integrity-seal failures before the same multi-instance cases are repeated
   against staging Firestore.
10. Application audit events are exported to a separately administered,
    retention-locked or otherwise tamper-evident logging sink and can be joined
    with Google Ads change events. The application ledger alone is not treated
    as forensic proof.

Then run `bash deploy/cloud-run.sh deploy-canary` with the production external
environment file. The tagged revision receives zero normal traffic. Check its
`/healthz`, `/readyz`, revision logs, image digest, runtime identity and secret
version references. OAuth callbacks use the configured stable origin, so a
traffic-tag URL alone is not evidence of an end-to-end OAuth test; that evidence
must come from staging with the same digest.

Only after Maikel, as release owner, signs both evidence sets may
`bash deploy/cloud-run.sh promote` be used. Immediately repeat unauthenticated,
Maikel-authenticated and Maria-authenticated read-only smoke tests on the stable
production URL. Roll back on any mismatch before enabling a live write.

## Monitoring and alerting

Create Cloud Monitoring alerts before promotion:

- Cloud Run request 5xx ratio above 2% for 5 minutes;
- p95 request latency above 10 seconds for 10 minutes;
- instance startup/probe failures or a zero-instance availability gap;
- uncaught exceptions and Google Ads authentication/quota failures;
- mutation outcomes in `UNCERTAIN` state for more than 5 minutes;
- any rejected wrong-account or unauthorized-approver attempt;
- Firestore permission, transaction-contention and write failures;
- Secret Manager access denied and unusual secret access volume.

Route alerts to at least two owners. A monitoring alert never authorizes an
automatic budget, bidding or targeting change.

Daily checks: OAuth availability, failed/uncertain changes, disapprovals,
broken conversion tracking and abnormal spend. Weekly checks: budget pacing,
search-term waste, lead quality, audit-ledger completeness and dependency
alerts. Monthly checks: IAM membership, OAuth consent users, secret age,
restore/rollback evidence, quota, cost and stale Firestore records.

## Incident response

1. Freeze new proposals and approvals. If account spend is at immediate risk,
   use the native Google Ads UI under an authorized human account to pause the
   exact campaign and document it.
2. Preserve Cloud Run, Cloud Audit Logs, the internal audit ledger and Google
   Ads change-event evidence. Never paste tokens or patient data into tickets.
3. For an application regression, choose the last known-good revision with
   `bash deploy/cloud-run.sh status`, set `ROLLBACK_REVISION`, then run
   `bash deploy/cloud-run.sh rollback`.
4. For suspected credential compromise, revoke affected OAuth grants, disable
   the secret version, create a new version, update the pinned version number
   and deploy a new zero-traffic revision. Rotating a signing/encryption key
   can invalidate sessions or pending changes; let pending changes expire and
   verify their terminal state before rotation.
5. Reconcile every change whose API outcome is unknown before retrying.
6. Record timeline, impact, exact Ads objects, spend exposure, containment,
   recovery and prevention. Maikel explicitly authorizes re-enabling writes.

## Backup, recovery and retention

- Configure scheduled Firestore backups/exports in the same EU data region and
  test restoration quarterly in a separate non-production project.
- Retain immutable release image digests and audit records according to the
  practice's legal/accounting requirements; define the period with the data
  protection adviser. Advertising audit records must not contain clinical
  data.
- Configure expiry/cleanup for OAuth state and abandoned change sets. Never
  delete an audit record merely to make a failed deployment look clean.
- Recovery objective: Cloud Run rollback within 15 minutes; no automatic
  replay of pending mutations after recovery.

## Secret and access rules

- No shared ChatGPT, Google, GitHub or Google Ads user accounts.
- Maria and Maikel are the only production operators; only Maikel is an
  approver. Sofia must appear in neither `GOOGLE_ADS_MCP_OPERATOR_EMAILS` nor
  `GOOGLE_ADS_MCP_APPROVER_EMAILS`.
- This is owner approval, not separation of duties: Maikel can request and
  approve a change. The system does not claim a four-eyes security guarantee.
- Runtime secrets exist only in Secret Manager. Deployment references numeric
  secret versions, not `latest`; rotation creates a reviewed new revision.
- `GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY` is an independent 32+ character
  secret; never reuse the approval-session or storage-encryption key.
- The runtime service account receives no Owner, Editor, Run Admin, Artifact
  Registry writer or project-wide Secret Accessor role.
- Human deployers use separate identities with MFA and only the deployment
  roles they need. Branch/environment review is an organizational release
  control and does not change the owner-approval authorization model.
- Review Google Cloud IAM, Google Ads users/managers, OAuth users and GitHub
  collaborators monthly and immediately after personnel changes.
