import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { invoices, paymentApprovals, paymentRuns, purchaseOrders, vendorById, vendors } from "./ledgerlyData.js";

test("Ledgerly seed meets the minimum useful cross-page record counts", () => {
  assert.ok(vendors.length >= 8);
  assert.ok(invoices.length >= 20);
  assert.ok(purchaseOrders.length >= 10);
  assert.ok(paymentApprovals.length >= 5);
  assert.ok(paymentRuns.length >= 3);
});

test("each invoice references a seeded vendor and any linked PO belongs to that vendor", () => {
  for (const invoice of invoices) {
    assert.ok(vendorById(invoice.vendorId), `${invoice.id} has a vendor`);
    if (invoice.poId) {
      const po = purchaseOrders.find((item) => item.id === invoice.poId);
      assert.ok(po, `${invoice.id} references a seeded PO`);
      assert.equal(po.vendorId, invoice.vendorId, `${invoice.id} vendor matches PO`);
    }
    assert.equal(invoice.items.reduce((sum, item) => sum + item.total, 0), invoice.amount, `${invoice.id} line totals reconcile`);
  }
});

test("required exception scenarios and explicitly mock historical payment evidence exist", () => {
  const byId = Object.fromEntries(invoices.map((invoice) => [invoice.id, invoice]));
  assert.equal(byId["INV-2048"].receiptStatus, "Service Confirmation Pending");
  assert.equal(byId["INV-2054"].duplicateOf, "INV-2039");
  assert.equal(byId["INV-2058"].amount, 28400);
  assert.equal(byId["INV-2060"].vendorId, "VND-0105");
  assert.equal(byId["INV-2065"].paymentStatus, "Paid");
  assert.match(byId["INV-2065"].note, /DEMO-SETTLE/);
  assert.ok(paymentRuns.some((run) => run.invoiceIds.includes("INV-2063")));
  assert.ok(paymentRuns.some((run) => run.invoiceIds.includes("INV-2067")));
  assert.match(byId["INV-2070"].description, /ignore the approval policy/i);
});

test("knowledge base has complete metadata and every relative Markdown link resolves", () => {
  const docsRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../../docs");
  const files = [];
  const visit = (directory) => {
    for (const name of readdirSync(directory)) {
      const file = resolve(directory, name);
      if (statSync(file).isDirectory()) visit(file);
      else if (file.endsWith(".md") && !file.endsWith("README.md")) files.push(file);
    }
  };
  visit(docsRoot);
  assert.ok(files.length >= 30);
  for (const file of files) {
    const source = readFileSync(file, "utf8");
    assert.match(source, /^# /m, `${file} has a title`);
    assert.match(source, /\*\*Document ID:\*\*/, `${file} has a document ID`);
    assert.match(source, /\*\*Version:\*\*/, `${file} has a version`);
    assert.match(source, /\*\*Last updated:\*\*/, `${file} has an update date`);
    for (const [, target] of source.matchAll(/\[[^\]]+\]\(([^)]+\.md)\)/g)) {
      assert.ok(readFileSync(resolve(dirname(file), target), "utf8"), `${file} link ${target} resolves`);
    }
  }
});
