# Ads manager capabilities

## Read and decision support

The manager provides account-scoped generic search plus typed read-only tools
for:

- account, campaign, network, budget, and conversion-action configuration;
- campaign performance with platform CPA/ROAS caveats;
- device, hour, weekday, network, geography, and keyword breakdowns;
- daily-budget pacing and estimated 30.4-day caps;
- costly search-term candidates without automatic exclusions;
- Google Ads change-event timelines with the explicit limitation that they are
  not browser/sign-in logs;
- ad policy restrictions and Google recommendations without auto-apply;
- deterministic period-over-period exception reports.

The manager can identify measurement gaps, wasted-spend candidates, delivery
anomalies, disapprovals, budget pressure, and configuration risks. It must not
call a campaign profitable until qualified inquiries, booked and attended
appointments, accepted treatment, and attributable value are reconciled.

## Guarded operations

Every supported write has a preview/apply pair, live-state drift detection,
`validate_only`, separate Maikel approval, single-use execution, immediate
post-read verification, and a durable ledger. Google Ads does not expose an
atomic compare-and-set across the state read and mutation; the system therefore
uses short approvals, protected non-target fingerprints, and post-verification
instead of claiming that the residual API race window is impossible.

The release supports:

- pause one enabled campaign;
- pause one enabled ad group while protecting against unreviewed child drift;
- pause/enable one ad while protecting its campaign and ad-group context;
- change one unshared average daily campaign budget, with increases capped at
  10 percent per approved change;
- pause/enable one keyword criterion after verifying its type;
- add one ad-group negative keyword with duplicate protection;
- remove one existing ad-group negative keyword after verifying its type and
  negative status;
- change one responsive-search-ad final URL through the correct immutable-ad
  API boundary, only when the global ad ID has exactly one non-removed
  ad-group use.

Recorded prior values support a separately previewed and approved rollback
where the typed workflow supports the reverse action. Campaign and ad-group
reactivation is deliberately manual in release 1 until bid-, criterion- and
asset-context queries pass real test-account contracts.
Read-only change-set tools expose the durable status and ordered audit timeline;
an `UNCERTAIN` result is never retried automatically and requires live-state
reconciliation.

## Intentionally not autonomous

The manager does not auto-apply Google recommendations, infer approval from
chat, retry uncertain writes, upload patient/customer data, change account
access, delete campaigns, or perform bulk mutations.

Bidding strategies, conversion-goal configuration, geographic/schedule/network
targeting, new ads/assets, experiments, Performance Max asset groups, customer
match, and offline conversions remain read-only or manual until each receives a
typed workflow, test-account contract tests, and a separate security review.

This boundary is deliberate: production-ready means the implemented surface is
safe and verifiable, not that every Google Ads API method is exposed to an LLM.
