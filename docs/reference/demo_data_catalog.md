# Demo data catalog

- **Document ID:** FIN-REF-005
- **Version:** 1.0
- **Document type:** Reference
- **Owner:** Finance Operations
- **Status:** Active (fictional)
- **Last updated:** 2026-10-01
- **Applies to:** Finance Operations Associate

The deterministic dataset is defined in `frontend/src/data/ledgerlyData.js`. It includes eight fictional vendors, 21 invoice records including historical duplicate comparator `INV-2039`, 11 POs, six approval requests, three payment runs, and 14 activity entries.

| Test case | Seeded record |
| --- | --- |
| Missing receipt | INV-2048 / PO-3101 |
| Approval pending | INV-2051 / APR-0301 |
| Above $25,000 tier | INV-2058 / APR-0302 |
| Possible duplicate | INV-2054 compared with INV-2039 |
| Missing PO | INV-2066 and INV-2070 |
| PO amount mismatch | INV-2068 / PO-3111 |
| Vendor verification pending | INV-2060 / VND-0105 |
| Vendor hold / bank review | INV-2062 / VND-0107 |
| Overdue | INV-2064 and INV-2073 |
| Historical paid mock | INV-2065 with DEMO-SETTLE-778 |
| Draft run inclusion | INV-2063 / PAY-0087 |
| Active run inclusion | INV-2067 / PAY-0088 |
| Untrusted instruction-like content | INV-2070 |

Money is fictional USD. Invoice line totals match invoice totals; deliberate PO, duplicate, receipt, and verification exceptions are called out on the records. For `PO-3103`, the invoiced-to-date amount counts the original seeded `INV-2039`; `INV-2054` is a duplicate candidate excluded from the PO matched total pending review. No full bank data exists.
