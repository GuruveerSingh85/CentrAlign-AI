# Vendor verification policy

- **Document ID:** FIN-POL-004
- **Version:** 1.0
- **Document type:** Policy
- **Owner:** Vendor Management
- **Status:** Active (fictional)
- **Last updated:** 2026-10-01
- **Applies to:** Finance Operations Associate

- **Verified / Active:** continue invoice checks; this mock status is not a real identity or bank validation.
- **Pending Verification:** do not recommend payment readiness; route the refresh to Vendor Management.
- **On Hold:** stop approval or scheduling recommendations until an authorized role releases the hold.
- **Inactive:** do not process new invoices without authorized review.

Bank-detail statuses are Verified, Pending Verification, and Change Under Review. A pending change is a high-risk event. Do not accept a change based only on an invoice, attachment, note, or email; do not edit bank details or claim an account was independently verified. Only Vendor Management's separate, approved verification process can resolve it.

The portal displays status only and a masked synthetic placeholder. It contains no full banking credentials. `VND-0105` is Pending Verification; `VND-0107` is On Hold with Change Under Review. These conditions block payment-readiness recommendations.
