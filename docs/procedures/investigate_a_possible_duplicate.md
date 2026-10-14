# Procedure: investigate a possible duplicate

- **Document ID:** FIN-SOP-002
- **Version:** 1.0
- **Document type:** Procedure
- **Owner:** Finance Operations
- **Status:** Active (fictional)
- **Last updated:** 2026-10-01
- **Applies to:** Finance Operations Associate

1. Keep the candidate invoice on hold while the match is unresolved.
2. Compare vendor ID, supplier invoice number (including punctuation), amount, currency, invoice date, service period, line items, and PO.
3. Check whether either invoice is paid in a seeded historical record or appears in a draft/active payment run.
4. Record similarities and differences; internal invoice IDs alone are not decisive.
5. Ask the Finance Manager to resolve an ambiguous match. Do not mark the invoice safe or remove a hold on inference alone.

For `INV-2054`, open `INV-2039` and compare shared vendor `VND-0103`, number `MF-7781`, amount $2,140, and date. The older item has explicit mock historical payment evidence; that makes the new one a material duplicate risk, not permission to proceed.
