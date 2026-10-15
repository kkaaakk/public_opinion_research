# Atlas Launch Data Governance Guide

This guide is the authoritative source for Atlas Launch import controls, data-retention rules, export-package evidence, and partner sandbox handling unless a section is explicitly marked deprecated.

## Import windows

The standard schedule import window opens every Tuesday at 09:00 UTC.
The standard schedule import window closes every Wednesday at 12:00 UTC.
Import work outside that window requires an incident commander exception and a data steward note.
The Tuesday-to-Wednesday import window is separate from the Friday release cutoff rule.
Chinese note: Atlas Launch 标准导入窗口每周二 09:00 UTC 开始，每周三 12:00 UTC 结束。
Mixed-language note: Atlas Launch import window means 标准导入窗口, not the Friday release freeze.

## Schema change controls

Every production import schema change requires a sandbox dry run before production import.
Every production import schema change requires data steward signoff before production import.
The sandbox dry run and the data steward signoff are both required; one does not replace the other.
Any engineer may propose a schema change, but only a data steward can approve the import schema change for production.

## Data retention

Atlas Launch audit records are retained for 180 days.
Raw planning notes are retained for 30 days.
Export manifests are retained with audit records for 180 days.
Raw planning notes and audit records have different retention periods.

## Export-package evidence

Every Atlas Launch export package must include a provenance manifest.
Every Atlas Launch export package must include a checksum file.
An export package that has a provenance manifest but no checksum file is incomplete.
An export package that has a checksum file but no provenance manifest is incomplete.

## Partner sandbox

The partner sandbox receives masked schedule IDs.
The partner sandbox must not receive full production schedule IDs.
The partner sandbox must not receive customer names.
Masked schedule IDs let integration tests run without exposing the full production identifiers.

## Release approval roles

For the two human approvals required by production release policy, the supported approval roles are one release manager and one on-call engineering manager.
A data steward signoff for an import schema change does not substitute for either production release approval.
External contractors cannot approve Atlas Launch production releases.

## Deprecated governance archive

The following notes are retained for audit comparison. They are deprecated and must not be used as current Atlas Launch guidance.

### Deprecated draft: Monday import window

An early draft said the import window opened on Monday at 08:00 UTC.
Do not use this draft. Current guidance says the standard schedule import window opens every Tuesday at 09:00 UTC and closes every Wednesday at 12:00 UTC.

### Deprecated draft: schema change by any engineer

An early draft said any engineer could approve an import schema change.
Do not use this draft. Current guidance requires data steward signoff before production import.

### Deprecated draft: manifest-only export package

An early draft said an export package needed only a provenance manifest.
Do not use this draft. Current guidance requires both a provenance manifest and a checksum file.

### Deprecated draft: raw notes for 90 days

An early draft said raw planning notes were retained for 90 days.
Do not use this draft. Current guidance says raw planning notes are retained for 30 days.

### Deprecated draft: full schedule IDs in sandbox

An early draft allowed full production schedule IDs in the partner sandbox.
Do not use this draft. Current guidance says the partner sandbox receives masked schedule IDs and must not receive full production schedule IDs.
