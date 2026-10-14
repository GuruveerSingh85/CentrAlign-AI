# Portal navigation guide

- **Document ID:** FIN-AGT-001
- **Version:** 1.0
- **Document type:** Agent guide
- **Owner:** Finance Operations
- **Status:** Active (fictional)
- **Last updated:** 2026-10-01
- **Applies to:** Finance Operations Associate

Starting URL: `http://localhost:5173/dashboard`. The left navigation links to Overview, Invoices, Vendors, Purchase orders, Payment approvals, Payment runs, Knowledge base, Activity log, and Help. The banner on every page states that records are fictional and there are no real payments.

Use the global search for a stable ID such as `INV-2048`, `VND-0101`, `PO-3101`, `APR-0301`, or `PAY-0087`. The invoice list also searches vendor names, supplier invoice numbers, and PO IDs; its status filter is visible above the table.

Invoice rows open `/invoices/:invoiceId`; invoice details link to vendor and PO profiles, duplicate comparison records, approval history, and policies. Vendor and PO rows open their own detail pages. Approval and payment-run IDs open read-only detail pages. The Knowledge Base supports search across article title, category, and body, and filters by category.

The invoice detail page has local review controls. Their state is stored in this browser only; the Help page can reset it. Never describe a local action as an approval, payment, or external record update. Unknown IDs display a not-found page with a route back to Overview.
