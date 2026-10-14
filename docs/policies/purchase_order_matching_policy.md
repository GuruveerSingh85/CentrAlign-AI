# Purchase-order matching policy

- **Document ID:** FIN-POL-003
- **Version:** 1.0
- **Document type:** Policy
- **Owner:** Finance Operations
- **Status:** Active (fictional)
- **Last updated:** 2026-10-01
- **Applies to:** Finance Operations Associate

The invoice vendor must equal the PO vendor, line items must fit the authorized scope, and required goods receipt or service confirmation must be present. A partial receipt covers only the recorded received quantity.

The seeded POs have no explicit variance tolerance. Therefore, any invoice/PO amount or scope mismatch requires investigation and human review. The fictional company may use a tolerance of 2% or $100, whichever is lower, only when the individual PO explicitly grants it; no seeded PO in this portal grants that tolerance. This rule is not a universal accounting standard.

Missing PO, vendor mismatch, amount overrun, or missing receipt requires a hold or information request. Never invent a PO or receipt, treat an unrecorded delivery as received, or alter a PO to force a match. `INV-2068` is $200 above `PO-3111` and remains on hold.
