# Payment approvals page guide

- **Document ID:** FIN-DASH-005
- **Version:** 1.0
- **Document type:** Dashboard guide
- **Owner:** Finance Operations
- **Status:** Active (fictional)
- **Last updated:** 2026-10-01
- **Applies to:** Finance Operations Associate

`/payment-approvals` is a simulated queue with approval ID, invoice or batch reference, vendor, amount, required tier, current state, assigned approver role, date, and attention reason. Open `/payment-approvals/:approvalId` for linked invoice checks, approval evidence, separation-of-duties reminder, and policies.

States include Awaiting Review, Pending Approver, Approved, Returned for Information, Rejected, and Expired. The Associate has no Approve or Reject control. An approval is complete only when the required independent human approvals appear in the record.
