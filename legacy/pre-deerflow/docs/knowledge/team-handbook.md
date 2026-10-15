# Atlas Launch Team Handbook

Atlas Launch is the internal satellite scheduling product used by the operations team.
This handbook is the authoritative source for Atlas Launch product scope, release controls, and governance cadence unless a section is explicitly marked deprecated.

## Product scope

The operations team uses Atlas Launch to coordinate internal satellite scheduling work.
Atlas Launch is not a customer-facing product; it supports internal scheduling and operational planning.
When a question asks what Atlas Launch is for, the supported answer is that it is an internal satellite scheduling product used by the operations team.

## Release controls

Every production release requires:

- two human approvals
- a documented rollback plan
- a release note entry before deployment begins

These production release prerequisites are release gates and must be satisfied before a production deployment starts.
The release note entry cannot be added after deployment begins.
A release is not ready for production deployment until all three release gates are complete.
The approvals, rollback plan, and release note entry are required together; none of the three replaces another.

### Release checklist examples

A valid release checklist includes:

- approval record one
- approval record two
- rollback owner assignment
- documented rollback plan
- release note entry prepared before deployment begins

A checklist that has a release note but no rollback plan is incomplete.
A checklist that has a rollback plan but only one human approval is incomplete.

## Governance

Quarterly risk reviews are mandatory for the Atlas Launch program.
The Atlas Launch program runs quarterly risk reviews before every seasonal planning cycle.
The team also reviews unresolved incidents during each quarterly planning cycle.

Risk reviews are quarterly, not monthly.
These sources do not say that unresolved incidents are reviewed only after production deployment.
Monthly cadence belongs to incident drills, not to risk reviews.
Governance review notes should distinguish risk reviews from incident drills because the two activities have different cadences.

## Deprecated guidance archive

The following notes are historical drafts retained for audit comparison. They are deprecated and must not be used as current Atlas Launch policy.

### Deprecated draft: single approval

An early draft said a production release could proceed with one approval.
Do not use this draft. Current policy requires two human approvals for every production release.

### Deprecated draft: monthly risk review

An early draft described monthly risk reviews.
Do not use this draft. Current policy says quarterly risk reviews are mandatory for the Atlas Launch program.

### Deprecated draft: release note after deployment

An early draft allowed the release note entry to be added after deployment begins.
Do not use this draft. Current policy requires a release note entry before deployment begins.

### Deprecated draft: unresolved incident timing

An early draft implied unresolved incidents were reviewed only after production deployment.
Do not use this draft. Current policy says the team reviews unresolved incidents during each quarterly planning cycle.
