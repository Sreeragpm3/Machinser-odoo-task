# Technical Note: Contract Progress Billing Module (`contract_billing`)

**Environment:** Odoo 19.0 Enterprise · Saudi Chart of Accounts (`l10n_sa`) · Currency: SAR  
**Author:** Sreerag PM  

---

### Overview & Approach

When approaching this task, my priority was to treat Odoo like an enterprise ERP rather than just writing custom tables for everything. In contracting, standard Odoo usually struggles because an invoice is either entirely paid or entirely unpaid—it doesn't have a native concept of "guarantee withheld until project handover" or "recovering 20% of an advance payment while keeping VAT based on total work done".

Instead of creating separate auxiliary journal vouchers or hacking core files, I designed `contract_billing` as a clean, self-contained addon that extends Odoo's native financial flow. All deductions happen directly within the invoice's own journal entry, meaning native Odoo aged receivable reports, general ledger, and partner balances stay accurate out of the box.

---

### 1. The Journal Entries

Here is how money actually moves across the accounts during the project lifecycle.

#### Step 1: Client pays the upfront advance (e.g., 200,000 SAR)
This uses standard Odoo Customer Payments (**Accounting ➔ Customers ➔ Payments**). No custom code touches this step; it creates an open credit on the client's receivable/advance account.
* **Debit:** Bank / Cash `200,000.00 SAR`
* **Credit:** Accounts Receivable / Client Advances `200,000.00 SAR`

---

#### Step 2: Progress Invoice confirmation (Work: 100k, VAT: 15k, Retention: 10%, Advance: 20%)
When the user validates the customer invoice, our `_post()` hook steps in right before standard validation and splits the receivable into three distinct lines:

| Account | Debit | Credit | What this line represents |
|---|:---:|:---:|---|
| **Contracting Revenue (Sales)** | — | 100,000.00 | Total work certified this cycle |
| **VAT Output (15%)** | — | 15,000.00 | Statutory tax liability (15% on gross work) |
| **Accounts Receivable** | **85,000.00** | — | **Amount Due Now** — what the client must pay right now |
| **Retention Receivable** | **10,000.00** | — | **Guarantee withheld** — maturity set to project handover date |
| **Advance Account / AR** | **20,000.00** | — | **Advance recovery** — immediately matched to payment |
| **Total** | **115,000.00** | **115,000.00** | Fully balanced entry |

**How the advance is settled:** Right after the entry posts, we take the 20,000 SAR recovery debit line and call `(line_a + line_b).reconcile()` against the advance payment's credit line. Odoo's payment widget immediately shows:
* *Paid on [Date]: 20,000 SAR*
* *Amount Due Now: 85,000 SAR*
* The remaining unamortized advance balance drops from 200,000 to 180,000 SAR automatically.

---

#### Step 3: Releasing the Retention at Project Handover
When warranty expires or the building is formally handed over, the accountant opens **Accounting ➔ Customers ➔ Release Retention**.
Selecting the customer pulls all posted invoices with unreleased retention. On confirmation, the wizard posts a non-cash reclassification entry in the **Miscellaneous Operations** journal:
* **Debit:** Accounts Receivable `10,000.00 SAR` *(now due for immediate collection)*
* **Credit:** Retention Receivable `10,000.00 SAR` *(clears the withheld balance to zero)*

---

#### Step 4: Credit Notes (Partial or Full Reversals)
If a progress invoice needs to be credited, Odoo's standard `_reverse_move_vals()` copies over the retention %, advance payment link, and recovery rate. When the credit note is confirmed, the exact same `_post()` pipeline runs with inverted signs:
* **Credit:** Accounts Receivable (cancels open due amount)
* **Credit:** Retention Receivable (reduces held retention)
* **Credit:** Advance line (re-credits customer's advance balance so they can reuse it)
* **Debit:** Revenue & VAT Output

---

### 2. Engineering & Design Choices

A few deliberate technical decisions made during implementation:

1. **Splitting inside the invoice vs separate Journal Entries:**  
   Creating a second separate journal entry for retention/advance is a common mistake that messes up reconciliation, doubles invoice numbers, and confuses accountants. Keeping everything in the invoice's own `line_ids` keeps the audit trail clean and ensures aged receivable reports show the exact collectible figure (`85k`).

2. **Idempotency and Reset to Draft:**  
   In Odoo 19, you cannot simply delete receivable lines on a move without passing `dynamic_unlink=True` in the context, otherwise Odoo's payment term consistency checks block the action. When a user clicks **Reset to Draft**, our `button_draft()` override:
   * Unreconciles the advance recovery partial reconcile.
   * Unlinks the injected retention and advance lines using `dynamic_unlink=True`.
   * Restores the primary Accounts Receivable line back to the full invoice balance.
   * Resets our `retention_lines_created` guard flag.  
   The user can edit numbers and re-confirm as many times as they like without duplicate lines or ledger corruption.

3. **Reusing Odoo's native reconciliation engine:**  
   Instead of writing custom math or tables to track advance drawdowns, we rely on standard partial reconciliation records (`account.partial.reconcile`). Odoo already handles exchange rates and partial balance tracking reliably; there is no need to reinvent the wheel.

4. **Multi-Company isolation by design:**  
   Retention accounts and default percentages are stored on `res.company` and surfaced through `res.config.settings`. This means multi-company environments are fully isolated out of the box.

---

### 3. Assumptions Made

1. **VAT applies to gross progress work:**  
   Under Saudi ZATCA and GCC VAT regulations, VAT is chargeable on the value of work performed (100k → 15k VAT). Financing deductions like retention withholding and advance recovery do not reduce the tax base.
2. **One advance payment per invoice:**  
   In contracting workflows, progress billings typically amortize against one primary project mobilization advance. To keep the UI intuitive for accountants, the invoice links to one `account.payment` record via a Many2one field. A single advance can, of course, be recovered across multiple consecutive invoices until depleted.
3. **Project definition:**  
   A "Project" is mapped to the first `sale.order` linked through the invoice lines (`invoice_line_ids.sale_line_ids.order_id`). If an invoice is created standalone without a sales order, the system cleanly falls back to grouping by Customer.
4. **Dedicated Retention Account:**  
   Assumed the company sets up a dedicated `asset_receivable` account (e.g., *Retention Receivable*) in Settings so retentions sit on a distinct balance sheet line separate from short-term trade debtors.

---

### 4. What Was Delivered & Tested

All requirements specified in the task are 100% complete and working:
* **Settings:** Retention account picker and default % on company configuration.
* **Invoice UI:** `Retention & Advance` tab with real-time recalculations and *"Amount Due Now"*.
* **Posting & Reconcile:** 5-line split and automatic reconciliation on confirmation.
* **Advance Cap Constraint:** Hard `@api.constrains` preventing advance recovery from exceeding remaining balance.
* **Handover Release Wizard:** Multi-invoice selection, partial/full release, and non-cash JE posting.
* **Credit Notes:** Seamless proportional reversals.
* **Draft Reset:** Safe idempotency handling.
* **Reporting:** Styled 9-column Excel report via `xlsxwriter` (Accounting ➔ Reporting ➔ Retention & Advance Report).
* **PDF Printout:** Clean QWeb inheritance showing the deductions summary table on customer invoice PDFs.
* **Unit Tests:** Full test suite in `tests/test_contract_billing.py` (`0 failed, 0 errors`).

#### Nice-to-have Future Enhancements (Beyond Interview Scope):
* **Multi-Advance Grid:** If a project has multiple concurrent mobilization advances, introducing a One2many allocation table would allow drawing down from several advance payments on a single invoice.
* **Handover Cron Alert:** An automated scheduled action to notify the project manager or contract administrator when an invoice's `retention_due_date` is approaching.
