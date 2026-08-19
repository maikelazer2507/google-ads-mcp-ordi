# Release readiness gates

## Required before merge

- all unit, schema, smoke, lint, dependency, and high-confidence security checks
  pass;
- test-account integration workflow is enabled and passes read-only contracts;
- independent engineering review covers approval/auth, Google Ads API
  semantics, privacy, failure handling, and deployment; this review does not
  imply runtime separation of duties;
- branch protection requires CI and review; no direct production push;
- all mutable image/action/dependency references are pinned or locked.

## Required before production deployment

- Secret Manager contains every secret listed in `PRODUCTION_RUNBOOK.md`;
- the session, storage-encryption, JWT-signing, and change-set-integrity keys
  are independent 32+ character values and numeric versions are pinned;
- the Cloud Run service account has only Secret Accessor for those secrets and
  least-privilege Firestore access;
- `/readyz` returns 200 only with explicit HTTP transport, production mode, a
  stable non-development environment, HTTPS MCP/approval origins, the manager
  ID, explicit read/write scopes, operator/approver scopes, and the redirect
  allowlist;
- OAuth and change-set storage both select Firestore, conflicting legacy
  aliases are absent, token encryption is enabled, and unscoped/sensitive
  reads are explicitly false;
- Google OAuth/GIS authorized origins and redirect URIs use the final Cloud Run
  domain, while FastMCP's client redirect allowlist contains only exact HTTPS
  ChatGPT callback URI(s), never a wildcard host;
- read and write allowlists contain only the externally configured production
  target customer;
- the manager/login customer matches the externally controlled deployment
  record;
- Maikel's verified Google email is the only initial approver;
- the operator allowlist contains only Maria and Maikel;
- Maria tests read/preview as operator and cannot approve;
- Maikel tests sign-in, exact review, confirmation, apply, replay rejection,
  audit, and a newly owner-approved rollback in the Google Ads test account;
- canary readiness, Firestore multi-instance transactions, stale execution, and
  `UNCERTAIN` reconciliation are exercised;
- Firestore Emulator transaction/replay/integrity tests pass, then equivalent
  multi-instance tests pass in a dedicated staging project;
- audit events are exported to a separately administered tamper-evident sink;
- existing Cloud Run revision remains available for immediate traffic rollback.

## Initial production pilot

Start with campaign pauses and budget decreases only. Observe the ledger
and Google Ads change events after every mutation. Enable other guarded actions
one at a time after successful test-account and canary evidence.

No API Center access-level change or additional Google Ads account write grant
is required for this release. Do not deploy a write-enabled revision until the
gates above are evidenced.

The authorization model is deliberately owner approval: Maria and Maikel are
operators, only Maikel approves, and Maikel may also be the requester. It does
not enforce requester/approver separation and must not be represented as a
four-eyes security guarantee.
