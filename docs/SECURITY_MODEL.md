# Security model

## Trust boundaries

The MCP client can read scoped advertising data, request previews, and submit an
already approved change token. It is not trusted to approve a change. Approval
occurs on a separate HTTPS route after Google OIDC verification and a second
confirmation step by Maikel of the exact persisted payload.

Maria is an operator. Maikel is the owner/approver. Roles and approver email are
assigned and checked server-side; tool arguments, chat text, HTTP headers, and
form fields cannot assign approval authority.

This is owner approval, not a four-eyes model. Maikel is also an operator and
can request and approve the same change. The runtime does not enforce distinct
requester and approver identities, so organizational review must not be
described as technical separation of duties.

## Protected assets

- Google Ads credentials and OAuth tokens;
- target customer scope and developer token quota;
- campaign state, budget, keywords, ads, and landing pages;
- exact approvals and the operational audit trail;
- search terms and conversion metadata that may reveal sensitive intent.

Patient diagnoses, treatment notes, clinical media, insurance data, free-text
records, and audience lists derived from health status are outside the system
and must never be uploaded.

## Controls

- Reads and writes fail closed to explicit customer allowlists.
- Remote service startup requires `GOOGLE_ADS_MCP_TRANSPORT=streamable-http`
  and the complete production readiness contract. Local stdio is available
  only with explicit `GOOGLE_ADS_MCP_TRANSPORT=stdio`,
  `GOOGLE_ADS_MCP_PRODUCTION_MODE=false`, and
  `GOOGLE_ADS_MCP_ENVIRONMENT=development`.
- OAuth identities also fail closed to an operator email allowlist; prior Ads
  account access alone does not authorize MCP use.
- Generic GAQL is bounded, syntactically constrained, and does not log filters.
- Lead-form, Local Services lead, and offline-user-data resources are blocked.
- Every write starts with a live read and Google Ads `validate_only` request.
- Change sets persist as `PENDING` with an opaque token hash, payload hash,
  customer, environment, requester, expiry, and an HMAC integrity seal backed
  by an independent Secret Manager key.
- Google OIDC verifies issuer, audience, expiry, subject, verified email, and the
  server-side approver allowlist.
- The approval page hides the payload until sign-in and requires a second,
  five-minute server-signed confirmation.
- Firestore transactions bind the expected action and atomically permit exactly
  one `APPROVED -> CONSUMED` transition.
- Apply re-reads protected state, validates again, mutates only the exact field,
  and verifies live state afterward.
- Audit events record ordered state transitions without tokens or raw exception
  payloads. Outcomes are `SUCCEEDED`, `FAILED`, or `UNCERTAIN`.
- An action mismatch does not consume an approval. Replay and concurrent apply
  attempts are rejected.

## Failure handling

- Drift before mutation: stop and create a new preview.
- API rejection before a mutation is accepted: record `FAILED`; do not invent a
  successful state.
- Mutation accepted but post-verification or audit completion unclear: record
  `UNCERTAIN`; never retry automatically. Reconcile the live object and ledger.
- Stale `IN_PROGRESS`: block retries and investigate by execution ID.
- Firestore unavailable: writes fail closed.
- Missing production configuration: `/readyz` returns 503.

Rollback is a new material change. Use the matching preview/apply pair with the
recorded prior value and obtain a new Maikel approval; never auto-rollback.
Campaign and ad-group reactivation is not model-callable in release 1 and must
remain manual until the full delivery-context contract is test-account proven.

## External production gates

- Run transaction, contention, replay, expiry and integrity-seal tests against
  Firestore Emulator, then repeat multi-instance tests in a dedicated staging
  Google Cloud project. Emulator success alone is insufficient evidence for
  production IAM, encryption or service behavior.
- Export structured application audit events to a separately administered,
  retention-locked or otherwise tamper-evident log sink and correlate them with
  Google Ads change events. The in-application Firestore ledger is not by
  itself independent, immutable forensic evidence.
- Production and staging use separate projects, service accounts, OAuth
  clients, secrets, Firestore data and Ads account allowlists.
