# Invoice status reference

- **Document ID:** FIN-REF-002
- **Version:** 1.0
- **Document type:** Reference
- **Owner:** Finance Operations
- **Status:** Active (fictional)
- **Last updated:** 2026-10-01
- **Applies to:** Finance Operations Associate

| Status | Meaning in this demo |
| --- | --- |
| Received | Seeded invoice is present; review is not complete. |
| Under Review | A review is in progress; no approval is implied. |
| Needs Information | Evidence is missing or a follow-up is needed. |
| Pending Approval | Required human approval is incomplete. |
| Approved | Seeded approval evidence exists; payment may still be unscheduled. |
| On Hold | An unresolved exception blocks recommendation. |
| Paid | Only a seeded historical mock record with explicit evidence. |

Overdue is a due-date condition calculated against the fixed demo date, not a review status. Payment status is separate: Not Scheduled, In Draft Run, In Active Run, or Paid. Local demo actions may add a local overlay and do not rewrite the source status or payment evidence.
