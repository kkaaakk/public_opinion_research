# Atlas Launch Misleading Archive

This file is intentionally adversarial. Every item below is deprecated, retired, misleading, or explicitly marked do_not_use. It exists only to test whether RAG retrieval and answer generation can resist plausible but wrong Atlas Launch claims.

Do not treat any misleading claim in this file as current policy. When an item includes a correction, the correction points back to the current authoritative guidance in the handbook, runbook, data governance guide, integration playbook, FAQ, or telemetry notes.

## single_hop misleading notes

1. [deprecated][single_hop] Misleading claim: Atlas Launch is a customer-facing booking portal. Correction: Atlas Launch is an internal satellite scheduling product used by the operations team.
2. [deprecated][single_hop] Misleading claim: The finance team is the primary user of Atlas Launch. Correction: The operations team uses Atlas Launch.
3. [deprecated][single_hop] Misleading claim: One human approval is enough for a production release. Correction: Every production release requires two human approvals.
4. [deprecated][single_hop] Misleading claim: A release note may be added after deployment begins. Correction: The release note entry is required before deployment begins.
5. [deprecated][single_hop] Misleading claim: The rollback plan is optional when there are two approvals. Correction: A documented rollback plan is required.
6. [deprecated][single_hop] Misleading claim: Risk reviews happen monthly. Correction: Risk reviews are quarterly.
7. [deprecated][single_hop] Misleading claim: Incident drills happen quarterly. Correction: On-call engineers run one incident drill every month.
8. [deprecated][single_hop] Misleading claim: Friday release cutoffs freeze after 18:00 UTC. Correction: They freeze after 17:00 UTC.
9. [deprecated][single_hop] Misleading claim: Any team lead can approve a Friday cutoff exception. Correction: An incident commander approves the exception.
10. [deprecated][single_hop] Misleading claim: Rollback owners may leave when deployment starts. Correction: They stay online until post-deployment verification is complete.
11. [deprecated][single_hop] Misleading claim: The standard import window opens Monday at 08:00 UTC. Correction: It opens Tuesday at 09:00 UTC.
12. [deprecated][single_hop] Misleading claim: The standard import window closes Thursday at 18:00 UTC. Correction: It closes Wednesday at 12:00 UTC.
13. [deprecated][single_hop] Misleading claim: Any engineer can approve production import schema changes. Correction: Only a data steward can approve them.
14. [deprecated][single_hop] Misleading claim: Audit records are retained for 90 days. Correction: Audit records are retained for 180 days.
15. [deprecated][single_hop] Misleading claim: Raw planning notes are retained for 180 days. Correction: Raw planning notes are retained for 30 days.
16. [deprecated][single_hop] Misleading claim: Export packages only need a provenance manifest. Correction: They need a provenance manifest and a checksum file.
17. [deprecated][single_hop] Misleading claim: Partner sandbox receives full production schedule IDs. Correction: It receives masked schedule IDs.
18. [deprecated][single_hop] Misleading claim: Partner Sync writes updates to production schedules. Correction: Partner Sync is read-only.
19. [deprecated][single_hop] Misleading claim: Backfill jobs run Saturday at 02:00 UTC. Correction: They run Sunday at 02:00 UTC.
20. [deprecated][single_hop] Misleading claim: The telemetry dashboard tracks job count only. Correction: It tracks queue depth, sync lag, and rollback readiness.
21. [deprecated][single_hop] Misleading claim: Atlas Launch is owned by customer success. Correction: Atlas Launch is used by the operations team for internal satellite scheduling.
22. [deprecated][single_hop] Misleading claim: Production release approvals are optional when the change is documentation-only. Correction: Every production release requires two human approvals.
23. [deprecated][single_hop] Misleading claim: The rollback plan may be replaced by a rollback owner assignment. Correction: A documented rollback plan is still required.
24. [deprecated][single_hop] Misleading claim: A release note entry is only recommended, not required. Correction: The release note entry is a required release gate before deployment begins.
25. [deprecated][single_hop] Misleading claim: Quarterly cadence applies to incident drills. Correction: Incident drills happen every month.
26. [deprecated][single_hop] Misleading claim: Monthly cadence applies to risk reviews. Correction: Risk reviews are quarterly.
27. [deprecated][single_hop] Misleading claim: Friday releases freeze only after 17:00 local time. Correction: The cutoff is after 17:00 UTC.
28. [deprecated][single_hop] Misleading claim: A release manager can approve a Friday cutoff exception. Correction: The runbook identifies the incident commander as the exception approver.
29. [deprecated][single_hop] Misleading claim: Post-deployment verification starts after rollback owners go offline. Correction: Rollback owners stay online until verification is complete.
30. [deprecated][single_hop] Misleading claim: The import window opens every Tuesday at 12:00 UTC. Correction: It opens every Tuesday at 09:00 UTC.
31. [deprecated][single_hop] Misleading claim: The import window closes every Wednesday at 09:00 UTC. Correction: It closes every Wednesday at 12:00 UTC.
32. [deprecated][single_hop] Misleading claim: Import work outside the window only needs an incident commander exception. Correction: It requires an incident commander exception and a data steward note.
33. [deprecated][single_hop] Misleading claim: A sandbox dry run alone approves production import schema changes. Correction: A sandbox dry run and data steward signoff are both required.
34. [deprecated][single_hop] Misleading claim: Export manifests are retained for 30 days with raw planning notes. Correction: Export manifests are retained with audit records for 180 days.
35. [deprecated][single_hop] Misleading claim: Partner sandbox tests may include full customer names if schedule IDs are masked. Correction: The partner sandbox must not receive customer names.
36. [deprecated][single_hop] Misleading claim: Partner Sync is a write-back integration for downstream planning tools. Correction: Partner Sync publishes read-only schedule snapshots.
37. [deprecated][single_hop] Misleading claim: New Partner Sync adapters need five calendar days of shadow mode. Correction: They need seven calendar days.
38. [deprecated][single_hop] Misleading claim: A sync incident bridge starts at exactly 15 minutes of lag. Correction: Sync lag above 15 minutes triggers the bridge.
39. [deprecated][single_hop] Misleading claim: Daily health summaries are generated at 10:30 UTC. Correction: They are generated at 09:30 UTC.
40. [deprecated][single_hop] Misleading claim: Disaster recovery tabletop exercises happen once a year. Correction: They happen twice a year.
41. [deprecated][single_hop] Misleading claim: Atlas Launch is primarily a telemetry dashboard. Correction: Atlas Launch is an internal satellite scheduling product used by the operations team.
42. [deprecated][single_hop] Misleading claim: The operations team only observes Atlas Launch and does not use it for scheduling. Correction: The operations team uses Atlas Launch to coordinate internal satellite scheduling work.
43. [deprecated][single_hop] Misleading claim: Production release gates include a backfill ticket. Correction: The release gates are two human approvals, a documented rollback plan, and a release note entry before deployment begins.
44. [deprecated][single_hop] Misleading claim: The two human approvals can be provided by any internal staff. Correction: The supported approval roles are one release manager and one on-call engineering manager.
45. [deprecated][single_hop] Misleading claim: External contractors can approve emergency production releases. Correction: External contractors cannot approve Atlas Launch production releases.
46. [deprecated][single_hop] Misleading claim: A data steward signoff is one of the two production release approvals. Correction: Data steward signoff does not substitute for either production release approval.
47. [deprecated][single_hop] Misleading claim: The incident commander owns all telemetry alerts immediately. Correction: The on-call engineer triages telemetry alerts before an incident is declared.
48. [deprecated][single_hop] Misleading claim: Sync incident updates stop when lag is exactly 15 minutes. Correction: Updates continue until sync lag returns below 15 minutes.
49. [deprecated][single_hop] Misleading claim: Sync incident status updates happen every 45 minutes. Correction: They are posted every 30 minutes.
50. [deprecated][single_hop] Misleading claim: The daily health summary is the only weekday dashboard review. Correction: The dashboard is reviewed every weekday morning, and the summary is generated at 09:30 UTC.
51. [deprecated][single_hop] Misleading claim: Telemetry dashboard metrics include customer names. Correction: The telemetry dashboard tracks queue depth, sync lag, and rollback readiness.
52. [deprecated][single_hop] Misleading claim: The partner sandbox receives production schedule IDs with customer names removed. Correction: It receives masked schedule IDs and must not receive full production schedule IDs.
53. [deprecated][single_hop] Misleading claim: Partner Sync shadow mode validates write permissions. Correction: Shadow mode is read-only and checks downstream consumption of masked schedule IDs.
54. [deprecated][single_hop] Misleading claim: Backfill tickets are optional for scheduled Sunday backfills. Correction: Each Atlas Launch backfill job requires a backfill ticket before it starts.
55. [deprecated][single_hop] Misleading claim: Manual backfills may run during a frozen Friday cutoff when the queue depth is low. Correction: They require an incident commander exception during a frozen Friday cutoff.
56. [deprecated][single_hop] Misleading claim: Post-deployment verification only checks audit logs. Correction: It includes canary comparison across the first 20 schedules and audit log sampling.
57. [deprecated][single_hop] Misleading claim: Export packages with customer-facing data do not need checksum files. Correction: Every Atlas Launch export package must include a checksum file.
58. [deprecated][single_hop] Misleading claim: The standard import window lasts until Wednesday 17:00 UTC. Correction: It closes every Wednesday at 12:00 UTC.
59. [deprecated][single_hop] Misleading claim: Any engineer can approve a schema change if the sandbox dry run passed. Correction: Only a data steward can approve the production import schema change.
60. [deprecated][single_hop] Misleading claim: Raw planning notes are retained longer than audit records. Correction: Raw planning notes are retained for 30 days and audit records for 180 days.

## multi_hop misleading notes

1. [retired][multi_hop] Misleading claim: A Friday release after 17:00 UTC only needs a team lead exception, and normal release gates no longer apply. Correction: It needs an incident commander exception, and the normal release gates still apply.
2. [retired][multi_hop] Misleading claim: Monthly risk reviews and quarterly incident drills form the governance cadence. Correction: Risk reviews are quarterly and incident drills are monthly.
3. [retired][multi_hop] Misleading claim: A release checklist is valid with one approval, a rollback owner, and a post-deployment release note. Correction: It needs two approvals, a documented rollback plan, and a release note before deployment begins.
4. [retired][multi_hop] Misleading claim: Import work outside the standard window can proceed with only a data steward note. Correction: It requires an incident commander exception and a data steward note.
5. [retired][multi_hop] Misleading claim: Schema changes need either a sandbox dry run or data steward signoff, not both. Correction: Both are required.
6. [retired][multi_hop] Misleading claim: Audit records and raw planning notes share the same 90-day retention period. Correction: Audit records are 180 days and raw planning notes are 30 days.
7. [retired][multi_hop] Misleading claim: Export packages are complete with a checksum file alone if the release note exists. Correction: Export packages need a provenance manifest and a checksum file.
8. [retired][multi_hop] Misleading claim: Partner sandbox tests use customer names plus masked schedule IDs. Correction: The sandbox must not receive customer names.
9. [retired][multi_hop] Misleading claim: New Partner Sync adapters can skip shadow mode if the sandbox uses masked IDs. Correction: They must run seven calendar days of read-only shadow mode.
10. [retired][multi_hop] Misleading claim: Manual backfills can run during a frozen Friday cutoff if a backfill ticket exists. Correction: They also require an incident commander exception.
11. [retired][multi_hop] Misleading claim: Sync lag above 5 minutes triggers a bridge owned by the on-call engineer. Correction: Sync lag above 15 minutes triggers a bridge, and the incident commander owns it after declaration.
12. [retired][multi_hop] Misleading claim: Sync incident status updates stop after the first 30-minute update. Correction: They continue every 30 minutes until lag returns below 15 minutes.
13. [retired][multi_hop] Misleading claim: Canary comparison alone completes post-deployment verification and releases rollback owners. Correction: Canary comparison and audit log sampling must both pass.
14. [retired][multi_hop] Misleading claim: Data steward signoff can replace the release manager approval for production releases. Correction: It does not substitute for production release approvals.
15. [retired][multi_hop] Misleading claim: Rollback readiness on the dashboard is the same artifact as the documented rollback plan. Correction: Rollback readiness is a telemetry metric; the rollback plan is a release gate.
16. [retired][multi_hop] Misleading claim: The daily health summary is generated at 08:00 UTC and replaces weekday dashboard review. Correction: It is generated at 09:30 UTC and the dashboard is reviewed every weekday morning.
17. [retired][multi_hop] Misleading claim: Disaster recovery tabletop exercises happen quarterly and are scheduled by the incident commander. Correction: They happen twice a year and are scheduled by the operations lead.
18. [retired][multi_hop] Misleading claim: Backfill tickets replace two human approvals when a backfill includes production deployment. Correction: Backfill tickets do not replace production release approvals.
19. [retired][multi_hop] Misleading claim: The import window and Friday release cutoff are the same control. Correction: They are separate controls.
20. [retired][multi_hop] Misleading claim: Seasonal planning risk reviews happen after unresolved incidents are reviewed post-deployment. Correction: Risk reviews happen before seasonal planning cycles, and unresolved incidents are reviewed during each quarterly planning cycle.
21. [retired][multi_hop] Misleading claim: A production release with a release manager approval and data steward signoff has the two required approvals. Correction: The required approvals are one release manager and one on-call engineering manager.
22. [retired][multi_hop] Misleading claim: A documented rollback plan can be skipped when rollback readiness appears on the telemetry dashboard. Correction: Rollback readiness is telemetry, and the documented rollback plan remains required.
23. [retired][multi_hop] Misleading claim: A Friday release exception removes the need for the rollback plan and release note. Correction: Friday exceptions do not remove normal release gates.
24. [retired][multi_hop] Misleading claim: A monthly incident drill satisfies the quarterly risk review requirement. Correction: Incident drills and risk reviews have separate cadences and purposes.
25. [retired][multi_hop] Misleading claim: Import work on Monday needs only a backfill ticket because it is before Tuesday. Correction: Work outside the Tuesday-to-Wednesday window requires an incident commander exception and a data steward note.
26. [retired][multi_hop] Misleading claim: A schema change can move to production after data steward signoff even if the sandbox dry run failed or did not happen. Correction: Both a sandbox dry run and data steward signoff are required.
27. [retired][multi_hop] Misleading claim: Audit records and export manifests follow the raw planning note retention period. Correction: Audit records and export manifests are retained for 180 days; raw planning notes are retained for 30 days.
28. [retired][multi_hop] Misleading claim: An export package with a checksum and masked schedule IDs satisfies all export evidence rules. Correction: Export evidence requires both a checksum file and provenance manifest.
29. [retired][multi_hop] Misleading claim: Partner sandbox data is safe if it includes customer names but not full production schedule IDs. Correction: The sandbox must not receive customer names or full production schedule IDs.
30. [retired][multi_hop] Misleading claim: Partner Sync shadow mode can write production schedules if writes are later audited. Correction: Shadow mode is read-only and Partner Sync does not change production schedules.
31. [retired][multi_hop] Misleading claim: The on-call engineer owns the sync incident bridge and status updates after declaration. Correction: The incident commander owns the bridge after an incident is declared.
32. [retired][multi_hop] Misleading claim: Sync incident updates are posted every 30 minutes until the canary comparison passes. Correction: Updates continue until sync lag returns below 15 minutes.
33. [retired][multi_hop] Misleading claim: Rollback owners may leave after the first 20 schedule canary comparison passes. Correction: They stay until both canary comparison and audit log sampling pass.
34. [retired][multi_hop] Misleading claim: The daily health summary at 09:30 UTC replaces manual weekday dashboard review. Correction: The dashboard is reviewed every weekday morning and the summary is generated at 09:30 UTC.
35. [retired][multi_hop] Misleading claim: Twice-yearly DR tabletop exercises replace monthly incident drills. Correction: DR tabletop exercises and monthly incident drills are separate activities.
36. [retired][multi_hop] Misleading claim: A data steward note for import work outside the window can replace the incident commander exception. Correction: Both are required outside the window.
37. [retired][multi_hop] Misleading claim: A backfill job on Sunday at 02:00 UTC can run without a backfill ticket if no release is active. Correction: Every backfill job requires a backfill ticket.
38. [retired][multi_hop] Misleading claim: A Friday manual backfill during a freeze only needs the normal backfill ticket. Correction: It also needs an incident commander exception.
39. [retired][multi_hop] Misleading claim: Production schedule changes from Partner Sync can be accepted after seven days of shadow mode. Correction: Partner Sync remains read-only after shadow mode.
40. [retired][multi_hop] Misleading claim: Quarterly planning reviews happen only when there are unresolved incidents. Correction: Quarterly risk reviews are mandatory before seasonal planning cycles.
41. [retired][multi_hop] Misleading claim: Because Atlas Launch is internal, release notes can be skipped if two approvals exist. Correction: Internal scope does not remove the required release note entry before deployment begins.
42. [retired][multi_hop] Misleading claim: If the on-call engineering manager approves a release, the rollback plan can be replaced by rollback readiness telemetry. Correction: The documented rollback plan is still required.
43. [retired][multi_hop] Misleading claim: A data steward signoff plus sandbox dry run also satisfies one production release approval. Correction: Schema controls are separate from production release approvals.
44. [retired][multi_hop] Misleading claim: Friday cutoff exceptions are approved by the release manager because the release manager is one approval role. Correction: The incident commander approves Friday cutoff exceptions.
45. [retired][multi_hop] Misleading claim: When a Friday cutoff exception is approved, manual backfills need no backfill ticket. Correction: Every backfill still requires a backfill ticket.
46. [retired][multi_hop] Misleading claim: A Sunday backfill at 02:00 UTC can write through Partner Sync after seven days of shadow mode. Correction: Backfills and Partner Sync are separate; Partner Sync remains read-only.
47. [retired][multi_hop] Misleading claim: Partner sandbox testing can include full production schedule IDs during read-only shadow mode. Correction: The sandbox receives masked schedule IDs and shadow mode is read-only.
48. [retired][multi_hop] Misleading claim: Customer names are allowed in partner sandbox data if Partner Sync does not update production schedules. Correction: The sandbox must not receive customer names.
49. [retired][multi_hop] Misleading claim: Sync lag above 15 minutes ends the bridge once the daily health summary is generated. Correction: Status updates continue every 30 minutes until lag returns below 15 minutes.
50. [retired][multi_hop] Misleading claim: The incident commander owns telemetry alert triage before an incident is declared and after declaration. Correction: The on-call engineer triages alerts before declaration; the incident commander owns the bridge after declaration.
51. [retired][multi_hop] Misleading claim: Post-deployment verification releases rollback owners after audit log sampling, even if canary comparison has not passed. Correction: Both canary comparison and audit log sampling must pass.
52. [retired][multi_hop] Misleading claim: The first 20 schedules canary comparison proves export package evidence is complete. Correction: Export packages separately require a provenance manifest and checksum file.
53. [retired][multi_hop] Misleading claim: Raw planning notes and export manifests both follow 30-day retention because they are planning artifacts. Correction: Export manifests are retained with audit records for 180 days.
54. [retired][multi_hop] Misleading claim: Quarterly risk reviews and twice-yearly DR tabletop exercises happen on the same governance schedule. Correction: Risk reviews are quarterly and DR tabletop exercises happen twice a year.
55. [retired][multi_hop] Misleading claim: Monthly incident drills replace reviewing unresolved incidents during quarterly planning. Correction: They are separate; unresolved incidents are reviewed during quarterly planning.
56. [retired][multi_hop] Misleading claim: Import work on Wednesday after 12:00 UTC can proceed with only a release note because it is close to the window. Correction: Work outside the window requires an incident commander exception and data steward note.
57. [retired][multi_hop] Misleading claim: A sandbox dry run can happen after production import if a data steward signs off first. Correction: Both sandbox dry run and data steward signoff are required before production import.
58. [retired][multi_hop] Misleading claim: If audit records are retained for 180 days, raw planning notes are also retained for 180 days for consistency. Correction: Raw planning notes are retained for 30 days.
59. [retired][multi_hop] Misleading claim: The telemetry dashboard's rollback readiness metric is the same evidence as post-deployment canary comparison. Correction: Rollback readiness is telemetry; canary comparison is part of post-deployment verification.
60. [retired][multi_hop] Misleading claim: Atlas Launch's internal product scope means partner sandbox masking is optional. Correction: Partner sandbox masking rules still require masked schedule IDs and no customer names.

## cross_lingual misleading notes

1. [do_not_use][cross_lingual] 错误说法: Atlas Launch 是面向客户的 booking portal。Correction: It is an internal satellite scheduling product for the operations team.
2. [do_not_use][cross_lingual] 错误说法: 生产发布只需要 one approval。Correction: It requires two human approvals.
3. [do_not_use][cross_lingual] 错误说法: rollback plan 可以部署后再补。Correction: A documented rollback plan is required before production deployment.
4. [do_not_use][cross_lingual] 错误说法: release note entry 可以 after deployment begins 再写。Correction: It must exist before deployment begins.
5. [do_not_use][cross_lingual] 错误说法: 风险评审是 monthly cadence。Correction: Risk reviews are quarterly.
6. [do_not_use][cross_lingual] 错误说法: incident drill 是 quarterly。Correction: On-call engineers run one incident drill every month.
7. [do_not_use][cross_lingual] 错误说法: Friday freeze 从 18:00 UTC 开始。Correction: It freezes after 17:00 UTC.
8. [do_not_use][cross_lingual] 错误说法: team lead 可以 approve Friday exception。Correction: The incident commander approves the exception.
9. [do_not_use][cross_lingual] 错误说法: rollback owners 部署开始后可以 offline。Correction: They stay online until post-deployment verification is complete.
10. [do_not_use][cross_lingual] 错误说法: 标准导入窗口 Monday 08:00 UTC open。Correction: The standard import window opens Tuesday at 09:00 UTC.
11. [do_not_use][cross_lingual] 错误说法: 导入窗口 Friday 17:00 UTC close。Correction: It closes Wednesday at 12:00 UTC.
12. [do_not_use][cross_lingual] 错误说法: schema change approval 可以由 any engineer 完成。Correction: Only a data steward can approve production import schema changes.
13. [do_not_use][cross_lingual] 错误说法: audit records 保留 30 days。Correction: Audit records are retained for 180 days.
14. [do_not_use][cross_lingual] 错误说法: raw planning notes 保留 180 days。Correction: Raw planning notes are retained for 30 days.
15. [do_not_use][cross_lingual] 错误说法: export package 只要 manifest。Correction: It must include a provenance manifest and a checksum file.
16. [do_not_use][cross_lingual] 错误说法: partner sandbox 可以收 customer names。Correction: It must not receive customer names.
17. [do_not_use][cross_lingual] 错误说法: Partner Sync 可以 write production schedules。Correction: Partner Sync is read-only.
18. [do_not_use][cross_lingual] 错误说法: 新 adapter 只要 three days shadow mode。Correction: It needs seven calendar days.
19. [do_not_use][cross_lingual] 错误说法: sync lag 超过 5 minutes 触发 bridge。Correction: The current threshold is above 15 minutes.
20. [do_not_use][cross_lingual] 错误说法: DR tabletop 是 quarterly。Correction: Disaster recovery tabletop exercises happen twice a year.
21. [do_not_use][cross_lingual] 错误说法: Atlas Launch belongs to customer success，而不是 operations team。Correction: It is used by the operations team.
22. [do_not_use][cross_lingual] 错误说法: prod release 只要 release manager approval。Correction: It needs two human approvals.
23. [do_not_use][cross_lingual] 错误说法: rollback owner assignment 可以替代 rollback plan。Correction: A documented rollback plan is required.
24. [do_not_use][cross_lingual] 错误说法: release note 是 nice-to-have，不是 gate。Correction: The release note entry is required before deployment begins.
25. [do_not_use][cross_lingual] 错误说法: 风险评审和 incident drill 都是 monthly。Correction: Risk reviews are quarterly and incident drills are monthly.
26. [do_not_use][cross_lingual] 错误说法: Friday cutoff exception 可以由 release manager approve。Correction: The incident commander approves it.
27. [do_not_use][cross_lingual] 错误说法: rollback owners can go offline after canary check。Correction: They stay online until full post-deployment verification completes.
28. [do_not_use][cross_lingual] 错误说法: import window 是 Tuesday 09:00 到 Thursday 12:00 UTC。Correction: It closes Wednesday at 12:00 UTC.
29. [do_not_use][cross_lingual] 错误说法: window 外导入只需要 data steward note。Correction: It also requires an incident commander exception.
30. [do_not_use][cross_lingual] 错误说法: sandbox dry run 可以替代 data steward signoff。Correction: Both are required.
31. [do_not_use][cross_lingual] 错误说法: audit records 保留 90 days，export manifest 保留 30 days。Correction: Audit records and export manifests are retained for 180 days.
32. [do_not_use][cross_lingual] 错误说法: export package 只要 checksum file。Correction: It also needs a provenance manifest.
33. [do_not_use][cross_lingual] 错误说法: partner sandbox 可以放 full schedule IDs if no customer names。Correction: It receives masked schedule IDs.
34. [do_not_use][cross_lingual] 错误说法: Partner Sync is write-enabled after shadow mode。Correction: Partner Sync is read-only.
35. [do_not_use][cross_lingual] 错误说法: shadow mode 是 7 business days。Correction: It is seven calendar days.
36. [do_not_use][cross_lingual] 错误说法: backfill jobs run Sunday 03:00 UTC。Correction: They run Sunday at 02:00 UTC.
37. [do_not_use][cross_lingual] 错误说法: sync incident status update 每 15 minutes。Correction: Updates are every 30 minutes.
38. [do_not_use][cross_lingual] 错误说法: incident commander owns bridge before declaration。Correction: The on-call engineer triages alerts before declaration.
39. [do_not_use][cross_lingual] 错误说法: daily health summary generated at 09:00 UTC。Correction: It is generated at 09:30 UTC.
40. [do_not_use][cross_lingual] 错误说法: DR tabletop twice-yearly means risk review twice-yearly。Correction: Risk reviews are quarterly.
41. [do_not_use][cross_lingual] 错误说法: Atlas Launch 是 telemetry-only dashboard。Correction: It is an internal satellite scheduling product.
42. [do_not_use][cross_lingual] 错误说法: operations team 只是 reviewer，不是 user。Correction: The operations team uses Atlas Launch.
43. [do_not_use][cross_lingual] 错误说法: release gates include backfill ticket。Correction: Release gates are approvals, rollback plan, and release note.
44. [do_not_use][cross_lingual] 错误说法: 两个 approvals 可以来自 any internal staff。Correction: The supported roles are release manager and on-call engineering manager.
45. [do_not_use][cross_lingual] 错误说法: contractor 可以 emergency approve production release。Correction: External contractors cannot approve production releases.
46. [do_not_use][cross_lingual] 错误说法: data steward signoff counts as production approval。Correction: It does not substitute for release approvals.
47. [do_not_use][cross_lingual] 错误说法: on-call engineer owns bridge after declaration。Correction: The incident commander owns it after declaration.
48. [do_not_use][cross_lingual] 错误说法: sync updates stop at exactly 15 minutes lag。Correction: Updates continue until lag is below 15 minutes.
49. [do_not_use][cross_lingual] 错误说法: status update cadence is 45 minutes。Correction: Sync incident updates are every 30 minutes.
50. [do_not_use][cross_lingual] 错误说法: daily health summary replaces weekday dashboard review。Correction: Dashboard review still happens every weekday morning.
51. [do_not_use][cross_lingual] 错误说法: dashboard metrics include customer names。Correction: Metrics are queue depth, sync lag, and rollback readiness.
52. [do_not_use][cross_lingual] 错误说法: partner sandbox can use full IDs with names removed。Correction: It receives masked schedule IDs.
53. [do_not_use][cross_lingual] 错误说法: shadow mode tests write permissions。Correction: Shadow mode is read-only.
54. [do_not_use][cross_lingual] 错误说法: scheduled Sunday backfill 不需要 ticket。Correction: Every backfill requires a ticket.
55. [do_not_use][cross_lingual] 错误说法: queue depth low means frozen Friday backfill can run。Correction: It still needs an incident commander exception.
56. [do_not_use][cross_lingual] 错误说法: post-deploy verification only audit log sampling。Correction: It also includes canary comparison across the first 20 schedules.
57. [do_not_use][cross_lingual] 错误说法: export package 不需要 checksum if manifest exists。Correction: It needs both checksum file and provenance manifest.
58. [do_not_use][cross_lingual] 错误说法: import window closes Wednesday 17:00 UTC。Correction: It closes Wednesday at 12:00 UTC.
59. [do_not_use][cross_lingual] 错误说法: any engineer approves schema after sandbox dry run。Correction: Only a data steward can approve production import schema changes.
60. [do_not_use][cross_lingual] 错误说法: raw planning notes kept longer than audit records。Correction: Raw planning notes are 30 days; audit records are 180 days.

## citation misleading notes

1. [misleading_citation][citation] Wrong citation hint: Cite the runbook for Atlas Launch product scope. Correction: Product scope is in the team handbook and FAQ.
2. [misleading_citation][citation] Wrong citation hint: Cite the telemetry notes for two human approvals. Correction: Release approvals are in the team handbook and FAQ.
3. [misleading_citation][citation] Wrong citation hint: Cite the FAQ for the seven-day Partner Sync shadow mode. Correction: The integration playbook contains that rule.
4. [misleading_citation][citation] Wrong citation hint: Cite the team handbook for the 15-minute sync lag threshold. Correction: The integration playbook contains that threshold.
5. [misleading_citation][citation] Wrong citation hint: Cite the runbook for raw planning note retention. Correction: The data governance guide contains retention rules.
6. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for the import window. Correction: The data governance guide contains the import window.
7. [misleading_citation][citation] Wrong citation hint: Cite data governance for rollback owners staying online. Correction: The operations runbook contains rollback-owner availability.
8. [misleading_citation][citation] Wrong citation hint: Cite integration playbook for quarterly risk reviews. Correction: The team handbook and FAQ contain risk review cadence.
9. [misleading_citation][citation] Wrong citation hint: Cite runbook for disaster recovery tabletop cadence. Correction: The telemetry notes contain DR tabletop cadence.
10. [misleading_citation][citation] Wrong citation hint: Cite FAQ for telemetry dashboard metrics. Correction: The telemetry notes list queue depth, sync lag, and rollback readiness.
11. [misleading_citation][citation] Wrong citation hint: Cite team handbook for masked schedule IDs. Correction: The data governance guide contains partner sandbox masking rules.
12. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for backfill jobs. Correction: The integration playbook contains backfill timing and ticket rules.
13. [misleading_citation][citation] Wrong citation hint: Cite FAQ for canary comparison across 20 schedules. Correction: The integration playbook contains post-deployment verification checks.
14. [misleading_citation][citation] Wrong citation hint: Cite runbook for release manager and on-call engineering manager approval roles. Correction: The data governance guide identifies the supported approval roles.
15. [misleading_citation][citation] Wrong citation hint: Cite data governance for incident drills. Correction: The runbook and FAQ identify monthly incident drills.
16. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for the Friday cutoff exception approver. Correction: The runbook and FAQ identify the incident commander.
17. [misleading_citation][citation] Wrong citation hint: Cite team handbook for export package evidence files. Correction: The data governance guide contains the provenance manifest and checksum file requirement.
18. [misleading_citation][citation] Wrong citation hint: Cite integration playbook for daily health summary generation time. Correction: The telemetry notes contain the 09:30 UTC time.
19. [misleading_citation][citation] Wrong citation hint: Cite runbook for import schema dry runs. Correction: The data governance guide contains schema change controls.
20. [misleading_citation][citation] Wrong citation hint: Cite FAQ for Partner Sync being read-only. Correction: The integration playbook contains Partner Sync behavior.
21. [misleading_citation][citation] Wrong citation hint: Cite data governance for Atlas Launch product scope. Correction: Product scope is in the team handbook and FAQ.
22. [misleading_citation][citation] Wrong citation hint: Cite integration playbook for production release gates. Correction: Release gates are in the team handbook and FAQ.
23. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for rollback plan requirements. Correction: The team handbook contains the rollback plan release gate.
24. [misleading_citation][citation] Wrong citation hint: Cite data governance for monthly incident drills. Correction: The runbook and FAQ contain incident drill cadence.
25. [misleading_citation][citation] Wrong citation hint: Cite runbook for quarterly risk reviews before seasonal planning. Correction: The team handbook and FAQ contain risk review cadence.
26. [misleading_citation][citation] Wrong citation hint: Cite team handbook for Friday cutoff exception ownership. Correction: The runbook and FAQ identify the incident commander.
27. [misleading_citation][citation] Wrong citation hint: Cite FAQ for rollback-owner availability. Correction: The runbook contains rollback-owner availability.
28. [misleading_citation][citation] Wrong citation hint: Cite integration playbook for the standard import window. Correction: The data governance guide contains the import window.
29. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for import work outside the standard window. Correction: The data governance guide contains the exception and data steward note requirements.
30. [misleading_citation][citation] Wrong citation hint: Cite FAQ for production import schema approvals. Correction: The data governance guide contains schema change controls.
31. [misleading_citation][citation] Wrong citation hint: Cite runbook for audit record retention. Correction: The data governance guide contains audit retention.
32. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for raw planning note retention. Correction: The data governance guide contains raw planning note retention.
33. [misleading_citation][citation] Wrong citation hint: Cite integration playbook for partner sandbox customer-name handling. Correction: The data governance guide contains partner sandbox restrictions.
34. [misleading_citation][citation] Wrong citation hint: Cite team handbook for Partner Sync shadow mode. Correction: The integration playbook contains shadow mode rules.
35. [misleading_citation][citation] Wrong citation hint: Cite runbook for backfill ticket requirements. Correction: The integration playbook contains backfill controls.
36. [misleading_citation][citation] Wrong citation hint: Cite data governance for sync incident status cadence. Correction: The integration playbook contains the 30-minute cadence.
37. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for post-deployment verification checks. Correction: The integration playbook contains canary comparison and audit log sampling.
38. [misleading_citation][citation] Wrong citation hint: Cite team handbook for rollback owners staying through audit sampling. Correction: The integration playbook and runbook contain rollback-owner availability and verification details.
39. [misleading_citation][citation] Wrong citation hint: Cite data governance for weekday dashboard review. Correction: The telemetry notes contain weekday dashboard review.
40. [misleading_citation][citation] Wrong citation hint: Cite runbook for disaster recovery tabletop ownership. Correction: The telemetry notes contain twice-yearly DR tabletop cadence.
41. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for Atlas Launch being internal scheduling software. Correction: Product scope is in the team handbook and FAQ.
42. [misleading_citation][citation] Wrong citation hint: Cite integration playbook for release note timing. Correction: The team handbook and FAQ contain release note timing.
43. [misleading_citation][citation] Wrong citation hint: Cite runbook for the supported production approval roles. Correction: The data governance guide identifies release manager and on-call engineering manager.
44. [misleading_citation][citation] Wrong citation hint: Cite team handbook for external contractor approval limits. Correction: The data governance guide says external contractors cannot approve production releases.
45. [misleading_citation][citation] Wrong citation hint: Cite integration playbook for data steward signoff not replacing release approvals. Correction: The data governance guide contains this distinction.
46. [misleading_citation][citation] Wrong citation hint: Cite data governance for on-call alert triage and bridge ownership. Correction: The integration playbook contains sync incident ownership.
47. [misleading_citation][citation] Wrong citation hint: Cite FAQ for sync incident 30-minute status updates. Correction: The integration playbook contains the 30-minute update cadence.
48. [misleading_citation][citation] Wrong citation hint: Cite runbook for telemetry dashboard metrics. Correction: The telemetry notes list queue depth, sync lag, and rollback readiness.
49. [misleading_citation][citation] Wrong citation hint: Cite data governance for daily health summary at 09:30 UTC. Correction: The telemetry notes contain the generation time.
50. [misleading_citation][citation] Wrong citation hint: Cite team handbook for Partner Sync read-only behavior. Correction: The integration playbook contains Partner Sync behavior.
51. [misleading_citation][citation] Wrong citation hint: Cite runbook for Partner Sync shadow mode length. Correction: The integration playbook contains the seven calendar day shadow mode rule.
52. [misleading_citation][citation] Wrong citation hint: Cite FAQ for Sunday backfill timing. Correction: The integration playbook contains backfill timing.
53. [misleading_citation][citation] Wrong citation hint: Cite data governance for backfill tickets. Correction: The integration playbook contains backfill ticket requirements.
54. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for manual backfills during Friday freeze. Correction: The integration playbook contains manual backfill freeze rules.
55. [misleading_citation][citation] Wrong citation hint: Cite runbook for canary comparison across first 20 schedules. Correction: The integration playbook contains post-deployment verification checks.
56. [misleading_citation][citation] Wrong citation hint: Cite team handbook for audit log sampling. Correction: The integration playbook contains audit log sampling.
57. [misleading_citation][citation] Wrong citation hint: Cite integration playbook for export manifest retention. Correction: The data governance guide contains export manifest retention.
58. [misleading_citation][citation] Wrong citation hint: Cite FAQ for Wednesday 12:00 UTC import window closure. Correction: The data governance guide contains import window timing.
59. [misleading_citation][citation] Wrong citation hint: Cite telemetry notes for production import schema approval roles. Correction: The data governance guide contains schema change controls.
60. [misleading_citation][citation] Wrong citation hint: Cite runbook for raw planning note retention. Correction: The data governance guide contains raw planning note retention.

## refutation misleading notes

1. [false_claim][refutation] False claim to refute: Atlas Launch is external customer software. Correct answer should refute this with internal operations scope.
2. [false_claim][refutation] False claim to refute: A single approval is enough for production release. Correct answer should refute this with two human approvals.
3. [false_claim][refutation] False claim to refute: Risk reviews are monthly. Correct answer should refute this with quarterly risk reviews.
4. [false_claim][refutation] False claim to refute: Incident drills are quarterly. Correct answer should refute this with monthly drills.
5. [false_claim][refutation] False claim to refute: Friday cutoff starts after 18:00 UTC. Correct answer should refute this with 17:00 UTC.
6. [false_claim][refutation] False claim to refute: Any team member may approve a Friday exception. Correct answer should refute this with incident commander approval.
7. [false_claim][refutation] False claim to refute: Rollback owners can leave after deployment starts. Correct answer should refute this with staying online until verification completes.
8. [false_claim][refutation] False claim to refute: The import window opens Monday at 08:00 UTC. Correct answer should refute this with Tuesday at 09:00 UTC.
9. [false_claim][refutation] False claim to refute: Any engineer can approve production import schema changes. Correct answer should refute this with data steward approval.
10. [false_claim][refutation] False claim to refute: Raw planning notes are retained for 90 days. Correct answer should refute this with 30 days.
11. [false_claim][refutation] False claim to refute: Export packages only need a manifest. Correct answer should refute this with manifest plus checksum file.
12. [false_claim][refutation] False claim to refute: Partner sandbox can receive full production schedule IDs. Correct answer should refute this with masked schedule IDs.
13. [false_claim][refutation] False claim to refute: Partner Sync updates production schedules. Correct answer should refute this with read-only behavior.
14. [false_claim][refutation] False claim to refute: New Partner Sync adapters need only three days of shadow mode. Correct answer should refute this with seven calendar days.
15. [false_claim][refutation] False claim to refute: Backfill jobs run Saturday at 02:00 UTC. Correct answer should refute this with Sunday at 02:00 UTC.
16. [false_claim][refutation] False claim to refute: Sync lag above 5 minutes triggers the current bridge. Correct answer should refute this with above 15 minutes.
17. [false_claim][refutation] False claim to refute: Canary comparison alone completes post-deployment verification. Correct answer should refute this with canary comparison and audit log sampling.
18. [false_claim][refutation] False claim to refute: Daily health summary is generated at 08:00 UTC. Correct answer should refute this with 09:30 UTC.
19. [false_claim][refutation] False claim to refute: DR tabletop exercises are quarterly. Correct answer should refute this with twice-yearly exercises.
20. [false_claim][refutation] False claim to refute: Data steward signoff replaces production release approvals. Correct answer should refute this because it does not substitute for release approvals.
21. [false_claim][refutation] False claim to refute: Atlas Launch supports external customer booking workflows. Correct answer should refute this with internal operations scheduling scope.
22. [false_claim][refutation] False claim to refute: Release note entries can be created after deployment if both approvals exist. Correct answer should refute this with release notes required before deployment begins.
23. [false_claim][refutation] False claim to refute: A rollback owner assignment is the same as a documented rollback plan. Correct answer should refute this because the documented rollback plan is required.
24. [false_claim][refutation] False claim to refute: The Friday release cutoff is part of the standard import window. Correct answer should refute this because they are separate controls.
25. [false_claim][refutation] False claim to refute: The import window closes Thursday at 12:00 UTC. Correct answer should refute this with Wednesday at 12:00 UTC.
26. [false_claim][refutation] False claim to refute: Import work outside the window can proceed with only an incident commander exception. Correct answer should refute this with incident commander exception plus data steward note.
27. [false_claim][refutation] False claim to refute: Data steward signoff alone completes schema change controls. Correct answer should refute this with sandbox dry run plus data steward signoff.
28. [false_claim][refutation] False claim to refute: Export manifests are retained for the raw planning note retention period. Correct answer should refute this with export manifests retained with audit records for 180 days.
29. [false_claim][refutation] False claim to refute: Customer names are allowed in the partner sandbox if schedule IDs are masked. Correct answer should refute this because customer names are not allowed.
30. [false_claim][refutation] False claim to refute: Partner Sync becomes writable after shadow mode. Correct answer should refute this with Partner Sync remaining read-only.
31. [false_claim][refutation] False claim to refute: Seven business days of shadow mode are required. Correct answer should refute this with seven calendar days.
32. [false_claim][refutation] False claim to refute: Backfill tickets replace release approvals. Correct answer should refute this because backfill tickets are operational evidence only.
33. [false_claim][refutation] False claim to refute: Manual backfills can run during a Friday freeze with a ticket alone. Correct answer should refute this with incident commander exception requirement.
34. [false_claim][refutation] False claim to refute: The on-call engineer owns the sync incident bridge after declaration. Correct answer should refute this with incident commander ownership.
35. [false_claim][refutation] False claim to refute: Sync incident status updates are every 15 minutes. Correct answer should refute this with every 30 minutes.
36. [false_claim][refutation] False claim to refute: Sync incident updates stop after lag reaches exactly 15 minutes. Correct answer should refute this with updates continuing until lag returns below 15 minutes.
37. [false_claim][refutation] False claim to refute: Audit log sampling is optional after canary comparison. Correct answer should refute this with both checks required.
38. [false_claim][refutation] False claim to refute: Weekday dashboard review is replaced by the daily health summary. Correct answer should refute this with both weekday review and 09:30 summary generation.
39. [false_claim][refutation] False claim to refute: DR tabletop exercises replace monthly incident drills. Correct answer should refute this because both are separate practices.
40. [false_claim][refutation] False claim to refute: External contractors can provide one of the two production release approvals. Correct answer should refute this because contractors cannot approve production releases.
41. [false_claim][refutation] False claim to refute: Atlas Launch is a telemetry-only status board. Correct answer should refute this with internal satellite scheduling scope.
42. [false_claim][refutation] False claim to refute: The operations team is not the user of Atlas Launch. Correct answer should refute this with operations team usage.
43. [false_claim][refutation] False claim to refute: Backfill tickets are production release gates. Correct answer should refute this with release gates being approvals, rollback plan, and release note.
44. [false_claim][refutation] False claim to refute: Any two internal employees can provide production approvals. Correct answer should refute this with release manager and on-call engineering manager roles.
45. [false_claim][refutation] False claim to refute: Data steward signoff can be counted as a release approval. Correct answer should refute this because it does not substitute for release approvals.
46. [false_claim][refutation] False claim to refute: The incident commander triages telemetry alerts before declaration. Correct answer should refute this with on-call engineer triage before declaration.
47. [false_claim][refutation] False claim to refute: Sync incident updates are every 45 minutes. Correct answer should refute this with every 30 minutes.
48. [false_claim][refutation] False claim to refute: The sync incident bridge closes when lag is exactly 15 minutes. Correct answer should refute this with lag returning below 15 minutes.
49. [false_claim][refutation] False claim to refute: The telemetry dashboard tracks customer names. Correct answer should refute this with queue depth, sync lag, and rollback readiness.
50. [false_claim][refutation] False claim to refute: Daily health summary generation replaces weekday dashboard review. Correct answer should refute this with both activities existing.
51. [false_claim][refutation] False claim to refute: Partner sandbox can receive full IDs if customer names are removed. Correct answer should refute this with masked schedule IDs.
52. [false_claim][refutation] False claim to refute: Partner Sync shadow mode tests write permissions. Correct answer should refute this with read-only shadow mode.
53. [false_claim][refutation] False claim to refute: Scheduled Sunday backfills do not need tickets. Correct answer should refute this with every backfill requiring a ticket.
54. [false_claim][refutation] False claim to refute: Low queue depth allows manual backfills during a frozen Friday cutoff. Correct answer should refute this with incident commander exception requirement.
55. [false_claim][refutation] False claim to refute: Post-deployment verification only checks audit logs. Correct answer should refute this with canary comparison and audit log sampling.
56. [false_claim][refutation] False claim to refute: Export packages are complete with only a checksum file. Correct answer should refute this with checksum plus provenance manifest.
57. [false_claim][refutation] False claim to refute: The import window closes Wednesday at 17:00 UTC. Correct answer should refute this with Wednesday at 12:00 UTC.
58. [false_claim][refutation] False claim to refute: Any engineer can approve schema changes after a sandbox dry run. Correct answer should refute this with data steward approval.
59. [false_claim][refutation] False claim to refute: Raw planning notes are retained longer than audit records. Correct answer should refute this with 30 days vs 180 days.
60. [false_claim][refutation] False claim to refute: Export manifests use raw planning note retention. Correct answer should refute this with export manifests retained with audit records for 180 days.

## edge_case misleading notes

1. [edge_case][misleading] Ambiguous term trap: prod deploy gates means only approval records. Correction: It means approvals, rollback plan, and release note entry.
2. [edge_case][misleading] Ambiguous term trap: release freeze means import window. Correction: Friday release cutoff and import window are separate controls.
3. [edge_case][misleading] Ambiguous term trap: availability after deployment means release note readiness. Correction: It refers to rollback owners staying online.
4. [edge_case][misleading] Ambiguous term trap: rollback readiness equals rollback plan. Correction: One is telemetry; the other is a required release artifact.
5. [edge_case][misleading] Ambiguous term trap: schedule IDs always means full production IDs. Correction: Partner sandbox uses masked schedule IDs.
6. [edge_case][misleading] Ambiguous term trap: shadow mode means optional testing. Correction: It is a required seven calendar day read-only period.
7. [edge_case][misleading] Ambiguous term trap: sync bridge owner is whoever triaged the alert. Correction: The incident commander owns the bridge after incident declaration.
8. [edge_case][misleading] Ambiguous term trap: below 15 minutes triggers bridge closure immediately without updates. Correction: Updates continue until sync lag returns below 15 minutes.
9. [edge_case][misleading] Ambiguous term trap: weekday review means daily health summary generation. Correction: Dashboard review happens every weekday morning; health summary is generated at 09:30 UTC.
10. [edge_case][misleading] Ambiguous term trap: twice-yearly cadence belongs to risk reviews. Correction: Twice-yearly applies to DR tabletop exercises.
11. [edge_case][misleading] Reverse-question trap: Which evidence file is not needed if checksum exists? Correction: Both checksum and provenance manifest are required.
12. [edge_case][misleading] Reverse-question trap: Which approval can contractors provide? Correction: External contractors cannot approve Atlas Launch production releases.
13. [edge_case][misleading] Reverse-question trap: What can replace the rollback plan? Correction: Nothing listed replaces the documented rollback plan.
14. [edge_case][misleading] Reverse-question trap: What can replace the data steward signoff? Correction: A sandbox dry run does not replace data steward signoff.
15. [edge_case][misleading] Reverse-question trap: What should happen if Friday is after 17:00 UTC and there is no exception? Correction: The release remains frozen.
16. [edge_case][misleading] Synonym trap: "prod push" needs only deployment readiness. Correction: Production deployment still requires the release gates.
17. [edge_case][misleading] Synonym trap: "schema migration" can be approved by the release manager. Correction: Production import schema change approval belongs to the data steward.
18. [edge_case][misleading] Synonym trap: "partner test env" can receive customer names. Correction: Partner sandbox must not receive customer names.
19. [edge_case][misleading] Synonym trap: "ops dashboard" tracks risk review cadence. Correction: Telemetry dashboard tracks queue depth, sync lag, and rollback readiness.
20. [edge_case][misleading] Synonym trap: "DR practice" is the monthly incident drill. Correction: DR tabletop happens twice a year; incident drills happen monthly.
21. [edge_case][misleading] Synonym trap: "launch portal" means customer-facing booking. Correction: Atlas Launch is internal operations scheduling software.
22. [edge_case][misleading] Synonym trap: "go-live signoff" means any two people can approve. Correction: The supported approval roles are release manager and on-call engineering manager.
23. [edge_case][misleading] Synonym trap: "revert owner" means rollback plan. Correction: Rollback owner assignment does not replace the documented rollback plan.
24. [edge_case][misleading] Synonym trap: "ship note" can be posted after deployment. Correction: The release note entry must exist before deployment begins.
25. [edge_case][misleading] Reverse-question trap: Which monthly governance review is required? Correction: Incident drills are monthly; risk reviews are quarterly.
26. [edge_case][misleading] Reverse-question trap: Who can approve a Friday exception if the incident commander is unavailable? Correction: The local sources identify only the incident commander.
27. [edge_case][misleading] Reverse-question trap: When can rollback owners disconnect after the canary comparison? Correction: Only after full post-deployment verification, including audit log sampling, is complete.
28. [edge_case][misleading] Ambiguous term trap: "import freeze" means the Friday release cutoff. Correction: Standard import window and Friday cutoff are separate controls.
29. [edge_case][misleading] Ambiguous term trap: "schema approval" means sandbox dry run. Correction: It means sandbox dry run plus data steward signoff for production import schema changes.
30. [edge_case][misleading] Ambiguous term trap: "retention evidence" means raw planning notes. Correction: Audit records and export manifests have a 180-day retention period.
31. [edge_case][misleading] Ambiguous term trap: "complete export package" means manifest or checksum. Correction: It requires both files.
32. [edge_case][misleading] Ambiguous term trap: "safe sandbox identifiers" means customer names are safe if IDs are masked. Correction: Customer names must not be sent to the partner sandbox.
33. [edge_case][misleading] Ambiguous term trap: "publish snapshots" means Partner Sync writes schedules. Correction: Partner Sync is read-only.
34. [edge_case][misleading] Reverse-question trap: Which day can a backfill run without a ticket? Correction: No listed day removes the backfill ticket requirement.
35. [edge_case][misleading] Synonym trap: "bridge threshold" means 15 minutes or more. Correction: The trigger is sync lag above 15 minutes.
36. [edge_case][misleading] Synonym trap: "bridge lead" means on-call engineer. Correction: The incident commander owns the bridge after declaration.
37. [edge_case][misleading] Ambiguous term trap: "status cadence" means risk reviews. Correction: The 30-minute cadence applies to sync incident status updates.
38. [edge_case][misleading] Reverse-question trap: Which post-deployment check can be skipped after first 20 schedules pass? Correction: Audit log sampling cannot be skipped.
39. [edge_case][misleading] Synonym trap: "morning health review" is generated at 09:30 UTC. Correction: The daily health summary is generated at 09:30 UTC; dashboard review happens every weekday morning.
40. [edge_case][misleading] Reverse-question trap: Which semiannual review covers seasonal planning risk? Correction: Seasonal planning risk reviews are quarterly, not semiannual.
41. [edge_case][misleading] Synonym trap: "status board" means Atlas Launch. Correction: Atlas Launch is internal satellite scheduling software, not just telemetry.
42. [edge_case][misleading] Ambiguous term trap: "operations view" means operations only observes the product. Correction: The operations team uses Atlas Launch for scheduling work.
43. [edge_case][misleading] Reverse-question trap: Which release gate can be replaced by a backfill ticket? Correction: None of the release gates are replaced by a backfill ticket.
44. [edge_case][misleading] Synonym trap: "human approval" means any human. Correction: Supported production approval roles are release manager and on-call engineering manager.
45. [edge_case][misleading] Reverse-question trap: Which contractor can approve an emergency production release? Correction: External contractors cannot approve production releases.
46. [edge_case][misleading] Ambiguous term trap: "data approval" means production release approval. Correction: Data steward signoff is schema-change approval, not a substitute for release approvals.
47. [edge_case][misleading] Synonym trap: "alert owner" means incident commander before declaration. Correction: The on-call engineer triages telemetry alerts before declaration.
48. [edge_case][misleading] Reverse-question trap: At what exact lag value does the sync bridge close? Correction: The sources say updates continue until lag returns below 15 minutes, not exactly at 15.
49. [edge_case][misleading] Ambiguous term trap: "30-minute cadence" means risk review schedule. Correction: It applies to sync incident status updates.
50. [edge_case][misleading] Synonym trap: "daily dashboard" means daily health summary only. Correction: The dashboard is reviewed every weekday morning and the daily health summary is generated at 09:30 UTC.
51. [edge_case][misleading] Ambiguous term trap: "rollback metric" means rollback plan completion. Correction: Rollback readiness is a telemetry metric, not the documented rollback plan.
52. [edge_case][misleading] Reverse-question trap: Which partner sandbox field can be full if names are omitted? Correction: Full production schedule IDs must not be sent.
53. [edge_case][misleading] Synonym trap: "shadow writes" means Partner Sync shadow mode. Correction: Shadow mode is read-only.
54. [edge_case][misleading] Reverse-question trap: Which scheduled backfill skips the ticket requirement? Correction: No scheduled backfill skips it.
55. [edge_case][misleading] Ambiguous term trap: "frozen release cutoff" applies only to production releases, not manual backfills. Correction: Manual backfills cannot run during the frozen cutoff without an incident commander exception.
56. [edge_case][misleading] Synonym trap: "verification sample" means audit logs only. Correction: Post-deployment verification includes canary comparison and audit log sampling.
57. [edge_case][misleading] Reverse-question trap: Which export evidence file can be omitted when the other exists? Correction: Neither provenance manifest nor checksum file can be omitted.
58. [edge_case][misleading] Ambiguous term trap: "Wednesday window" includes the whole Wednesday workday. Correction: The standard import window closes Wednesday at 12:00 UTC.
59. [edge_case][misleading] Reverse-question trap: Who approves schema changes when the data steward is absent? Correction: The local sources identify only data steward approval.
60. [edge_case][misleading] Synonym trap: "planning retention" means all planning-related artifacts are 30 days. Correction: Raw planning notes are 30 days, but export manifests and audit records are 180 days.

## negative misleading notes

1. [unanswerable_trap][negative] Misleading claim: The SSO provider is Okta. Do not use; the local knowledge base has no cited evidence for SSO provider.
2. [unanswerable_trap][negative] Misleading claim: Production schedules are stored in PostgreSQL. Do not use; the local knowledge base has no cited evidence for the database.
3. [unanswerable_trap][negative] Misleading claim: The cafeteria menu is maintained by the operations team. Do not use; the local knowledge base has no cited evidence for cafeteria menus.
4. [unanswerable_trap][negative] Misleading claim: Report generation must use GPT-4. Do not use; the local knowledge base has no cited evidence for model providers.
5. [unanswerable_trap][negative] Misleading claim: Post-deployment verification has a 20-minute SLA. Do not use; the local knowledge base has no cited evidence for an SLA.
6. [unanswerable_trap][negative] Misleading claim: Atlas Launch runs on AWS us-east-1. Do not use; the local knowledge base has no cited evidence for cloud provider or region.
7. [unanswerable_trap][negative] Misleading claim: The product manager is Dana Wu. Do not use; the local knowledge base has no cited evidence for product manager.
8. [unanswerable_trap][negative] Misleading claim: Incident severities are Sev0 through Sev4. Do not use; the local knowledge base has no cited evidence for severity levels.
9. [unanswerable_trap][negative] Misleading claim: HR owns staffing approvals for Atlas Launch. Do not use; the local knowledge base has no cited evidence for HR staffing ownership.
10. [unanswerable_trap][negative] Misleading claim: Export checksums use SHA-512. Do not use; the local knowledge base has no cited evidence for the checksum algorithm.
11. [unanswerable_trap][negative] Misleading claim: Audit records are stored in table atlas_audit_events. Do not use; the local knowledge base has no cited evidence for database table names.
12. [unanswerable_trap][negative] Misleading claim: Sync incidents use Slack channel #atlas-sync-war-room. Do not use; the local knowledge base has no cited evidence for Slack channel names.
13. [unanswerable_trap][negative] Misleading claim: Legal counsel Priya Shah approves sandbox data sharing. Do not use; the local knowledge base has no cited evidence for legal counsel.
14. [unanswerable_trap][negative] Misleading claim: Import batches are capped at 10,000 rows. Do not use; the local knowledge base has no cited evidence for import batch row limits.
15. [unanswerable_trap][negative] Misleading claim: Daily health summary review happens in room Orbit-12. Do not use; the local knowledge base has no cited evidence for office rooms.
16. [unanswerable_trap][negative] Misleading claim: Partner Sync is hosted in Azure West US 3. Do not use; the local knowledge base has no cited evidence for hosting region.
17. [unanswerable_trap][negative] Misleading claim: The on-call rotation has eight engineers. Do not use; the local knowledge base has no cited evidence for team size.
18. [unanswerable_trap][negative] Misleading claim: Pager escalation waits seven minutes. Do not use; the local knowledge base has no cited evidence for pager escalation timing.
19. [unanswerable_trap][negative] Misleading claim: The release calendar is owned by vendor Starline. Do not use; the local knowledge base has no cited evidence for vendors.
20. [unanswerable_trap][negative] Misleading claim: The partner sandbox retention policy is 14 days. Do not use; the local knowledge base has no cited evidence for sandbox retention.
21. [unanswerable_trap][negative] Misleading claim: Atlas Launch uses Pinecone as its production vector database. Do not use; the local knowledge base has no cited evidence for vector databases.
22. [unanswerable_trap][negative] Misleading claim: The official embedding model is bge-m3. Do not use; the local knowledge base has no cited evidence for embedding models.
23. [unanswerable_trap][negative] Misleading claim: The reranker must be Cohere Rerank v3. Do not use; the local knowledge base has no cited evidence for reranker providers.
24. [unanswerable_trap][negative] Misleading claim: Atlas Launch production uses Kubernetes namespace atlas-prod. Do not use; the local knowledge base has no cited evidence for Kubernetes namespaces.
25. [unanswerable_trap][negative] Misleading claim: The release dashboard is hosted at atlas.internal.example. Do not use; the local knowledge base has no cited evidence for dashboard URLs.
26. [unanswerable_trap][negative] Misleading claim: The incident commander is Morgan Lee. Do not use; the local knowledge base has no cited evidence for named personnel.
27. [unanswerable_trap][negative] Misleading claim: The on-call engineering manager is rotated every six weeks. Do not use; the local knowledge base has no cited evidence for rotation length.
28. [unanswerable_trap][negative] Misleading claim: Release approvals expire after 24 hours. Do not use; the local knowledge base has no cited evidence for approval expiration.
29. [unanswerable_trap][negative] Misleading claim: Canary comparison failures are escalated to finance. Do not use; the local knowledge base has no cited evidence for finance escalation.
30. [unanswerable_trap][negative] Misleading claim: Audit log sampling checks exactly 50 audit events. Do not use; the local knowledge base has no cited evidence for sampling size.
31. [unanswerable_trap][negative] Misleading claim: The checksum file must use SHA-256. Do not use; the local knowledge base has no cited evidence for checksum algorithms.
32. [unanswerable_trap][negative] Misleading claim: Partner Sync publishes snapshots every five minutes. Do not use; the local knowledge base has no cited evidence for snapshot frequency.
33. [unanswerable_trap][negative] Misleading claim: Downstream planning tools are Snowflake and Tableau. Do not use; the local knowledge base has no cited evidence for named downstream tools.
34. [unanswerable_trap][negative] Misleading claim: The backfill ticket prefix is ATLAS-BF. Do not use; the local knowledge base has no cited evidence for ticket prefixes.
35. [unanswerable_trap][negative] Misleading claim: Friday cutoff exceptions require VP approval. Do not use; the local knowledge base has no cited evidence for VP approval.
36. [unanswerable_trap][negative] Misleading claim: Quarterly planning cycles begin on the first Monday of each quarter. Do not use; the local knowledge base has no cited evidence for exact planning dates.
37. [unanswerable_trap][negative] Misleading claim: DR tabletop exercises are run in the staging environment. Do not use; the local knowledge base has no cited evidence for tabletop environments.
38. [unanswerable_trap][negative] Misleading claim: Raw planning notes are stored in Google Drive. Do not use; the local knowledge base has no cited evidence for storage systems.
39. [unanswerable_trap][negative] Misleading claim: Customer names are hashed with HMAC before sandbox use. Do not use; the local knowledge base has no cited evidence for hashing methods.
40. [unanswerable_trap][negative] Misleading claim: The operations team has a ten-person approval quorum. Do not use; the local knowledge base has no cited evidence for approval quorum size.
41. [unanswerable_trap][negative] Misleading claim: The production release form is named AL-Release-42. Do not use; the local knowledge base has no cited evidence for form names.
42. [unanswerable_trap][negative] Misleading claim: Atlas Launch stores release notes in Confluence. Do not use; the local knowledge base has no cited evidence for documentation tools.
43. [unanswerable_trap][negative] Misleading claim: The Milvus collection is named atlas_launch_prod. Do not use; the local knowledge base has no cited evidence for collection names.
44. [unanswerable_trap][negative] Misleading claim: The BM25 keyword index uses Elasticsearch in production. Do not use; the local knowledge base has no cited evidence for production search infrastructure.
45. [unanswerable_trap][negative] Misleading claim: Rollback owners use PagerDuty schedule Atlas-RB. Do not use; the local knowledge base has no cited evidence for PagerDuty schedule names.
46. [unanswerable_trap][negative] Misleading claim: The release manager approval must come from the Orbit group. Do not use; the local knowledge base has no cited evidence for group names.
47. [unanswerable_trap][negative] Misleading claim: The on-call engineering manager role is assigned in Jira. Do not use; the local knowledge base has no cited evidence for assignment systems.
48. [unanswerable_trap][negative] Misleading claim: Import dry runs use the schema file atlas_schema_v7.yml. Do not use; the local knowledge base has no cited evidence for schema file names.
49. [unanswerable_trap][negative] Misleading claim: Data steward notes must be written in Markdown. Do not use; the local knowledge base has no cited evidence for note file formats.
50. [unanswerable_trap][negative] Misleading claim: Export manifests are encrypted with AES-256. Do not use; the local knowledge base has no cited evidence for encryption algorithms.
51. [unanswerable_trap][negative] Misleading claim: Backfill tickets are reviewed by a database administrator named Chen. Do not use; the local knowledge base has no cited evidence for database administrators or named reviewers.
52. [unanswerable_trap][negative] Misleading claim: Sync lag telemetry is sampled every 10 seconds. Do not use; the local knowledge base has no cited evidence for telemetry sampling frequency.
53. [unanswerable_trap][negative] Misleading claim: Queue depth alerts page the SRE primary. Do not use; the local knowledge base has no cited evidence for paging routes.
54. [unanswerable_trap][negative] Misleading claim: The daily health summary is emailed to <atlas-ops@example.com>. Do not use; the local knowledge base has no cited evidence for email aliases.
55. [unanswerable_trap][negative] Misleading claim: DR tabletop exercises are facilitated by vendor Northstar. Do not use; the local knowledge base has no cited evidence for facilitation vendors.
56. [unanswerable_trap][negative] Misleading claim: Canary comparison uses a tolerance threshold of 0.01%. Do not use; the local knowledge base has no cited evidence for tolerance thresholds.
57. [unanswerable_trap][negative] Misleading claim: Audit log sampling is performed with SQL query audit_sample.sql. Do not use; the local knowledge base has no cited evidence for SQL query names.
58. [unanswerable_trap][negative] Misleading claim: The partner sandbox is refreshed every Monday. Do not use; the local knowledge base has no cited evidence for sandbox refresh cadence.
59. [unanswerable_trap][negative] Misleading claim: Masked schedule IDs use prefix MASK-. Do not use; the local knowledge base has no cited evidence for masked ID formats.
60. [unanswerable_trap][negative] Misleading claim: Seasonal planning cycles are named Spring, Summer, Fall, and Winter. Do not use; the local knowledge base has no cited evidence for planning cycle names.
