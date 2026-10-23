# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError, UserError
from odoo import fields


@tagged('post_install', '-at_install')
class TestContractBilling(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        # Ensure currency has rounding
        cls.currency = cls.company.currency_id

        # Retention Receivable Account
        cls.retention_account = cls.env['account.account'].create({
            'name': 'Test Retention Receivable',
            'code': '102099',
            'account_type': 'asset_receivable',
            'reconcile': True,
            'company_ids': [(6, 0, cls.company.ids)],
        })
        cls.company.retention_receivable_account_id = cls.retention_account
        cls.company.retention_default_percent = 10.0

        # Customer
        cls.partner = cls.env['res.partner'].create({
            'name': 'Saudi Construction Client',
            'customer_rank': 1,
        })

        # Sales Journal
        cls.sale_journal = cls.env['account.journal'].search([
            ('type', '=', 'sale'),
            ('company_id', '=', cls.company.id),
        ], limit=1)

        # Bank Journal
        cls.bank_journal = cls.env['account.journal'].search([
            ('type', '=', 'bank'),
            ('company_id', '=', cls.company.id),
        ], limit=1)

        # Product for Contracting Work
        cls.work_product = cls.env['product.product'].create({
            'name': 'Contracting Work Progress',
            'type': 'service',
            'list_price': 100000.0,
        })

        # 15% VAT Tax
        cls.tax_15 = cls.env['account.tax'].search([
            ('type_tax_use', '=', 'sale'),
            ('amount', '=', 15.0),
            ('company_id', '=', cls.company.id),
        ], limit=1)
        if not cls.tax_15:
            cls.tax_15 = cls.env['account.tax'].create({
                'name': 'VAT 15%',
                'amount_type': 'percent',
                'amount': 15.0,
                'type_tax_use': 'sale',
                'company_id': cls.company.id,
            })

    def _create_advance_payment(self, amount=200000.0):
        """Create a 200,000 SAR customer advance payment with no invoice."""
        payment = self.env['account.payment'].create({
            'partner_id': self.partner.id,
            'amount': amount,
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'journal_id': self.bank_journal.id,
            'date': fields.Date.today(),
        })
        payment.action_post()
        return payment

    def test_01_retention_and_advance_computation(self):
        """Verify computed amounts match the example:
           Work done: 100,000 | VAT 15%: 15,000 | Total: 115,000
           Less Retention (10%): 10,000 | Less Advance (20%): 20,000
           Amount to pay now: 85,000
        """
        payment = self._create_advance_payment(200000.0)

        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'journal_id': self.sale_journal.id,
            'invoice_date': fields.Date.today(),
            'retention_percent': 10.0,
            'advance_payment_id': payment.id,
            'advance_recovery_type': 'percent',
            'advance_recovery_percent': 20.0,
            'invoice_line_ids': [
                (0, 0, {
                    'product_id': self.work_product.id,
                    'price_unit': 100000.0,
                    'quantity': 1.0,
                    'tax_ids': [(6, 0, self.tax_15.ids)],
                }),
            ],
        })

        self.assertAlmostEqual(invoice.amount_untaxed, 100000.0)
        self.assertAlmostEqual(invoice.amount_tax, 15000.0)
        self.assertAlmostEqual(invoice.amount_total, 115000.0)
        self.assertAlmostEqual(invoice.retention_amount, 10000.0)
        self.assertAlmostEqual(invoice.advance_recovery_amount, 20000.0)
        self.assertAlmostEqual(invoice.amount_to_pay_now, 85000.0)
        self.assertAlmostEqual(invoice.advance_remaining, 180000.0)

    def test_02_invoice_posting_and_split(self):
        """Verify journal lines when posted:
           - Normal AR line = 85,000
           - Retention Receivable line = 10,000 (with handover maturity date)
           - Advance line = 20,000 (reconciled with advance payment)
        """
        payment = self._create_advance_payment(200000.0)
        handover_date = fields.Date.add(fields.Date.today(), days=365)

        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'journal_id': self.sale_journal.id,
            'invoice_date': fields.Date.today(),
            'retention_percent': 10.0,
            'retention_due_date': handover_date,
            'advance_payment_id': payment.id,
            'advance_recovery_type': 'percent',
            'advance_recovery_percent': 20.0,
            'invoice_line_ids': [
                (0, 0, {
                    'product_id': self.work_product.id,
                    'price_unit': 100000.0,
                    'quantity': 1.0,
                    'tax_ids': [(6, 0, self.tax_15.ids)],
                }),
            ],
        })

        invoice.action_post()

        # Check retention flag
        self.assertTrue(invoice.retention_lines_created)

        # Check Retention Line
        ret_line = invoice.line_ids.filtered(lambda l: l.cb_is_retention_line)
        self.assertTrue(ret_line)
        self.assertEqual(ret_line.account_id, self.retention_account)
        self.assertAlmostEqual(ret_line.debit, 10000.0)
        self.assertEqual(ret_line.date_maturity, handover_date)

        # Check Advance Line
        adv_line = invoice.line_ids.filtered(lambda l: l.cb_is_advance_recovery_line)
        self.assertTrue(adv_line)
        self.assertAlmostEqual(adv_line.debit, 20000.0)
        self.assertTrue(adv_line.reconciled)

        # Check Normal AR Line
        ar_line = invoice.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
                      and not l.cb_is_retention_line
                      and not l.cb_is_advance_recovery_line
        )
        self.assertAlmostEqual(ar_line.debit, 85000.0)

    def test_03_advance_recovery_cap(self):
        """Verify ValidationError when recovering more than remaining advance balance."""
        payment = self._create_advance_payment(15000.0)  # only 15,000 advance

        with self.assertRaises(ValidationError):
            self.env['account.move'].create({
                'move_type': 'out_invoice',
                'partner_id': self.partner.id,
                'journal_id': self.sale_journal.id,
                'invoice_date': fields.Date.today(),
                'advance_payment_id': payment.id,
                'advance_recovery_type': 'fixed',
                'advance_recovery_amount': 25000.0,  # exceeds 15,000!
                'invoice_line_ids': [
                    (0, 0, {
                        'product_id': self.work_product.id,
                        'price_unit': 100000.0,
                        'quantity': 1.0,
                    }),
                ],
            })

    def test_04_reset_to_draft_and_repost(self):
        """Verify resetting to draft removes injected lines, unreconciles advance,
           and re-posting doesn't duplicate lines (Req 7 – idempotency).
        """
        payment = self._create_advance_payment(200000.0)

        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'journal_id': self.sale_journal.id,
            'invoice_date': fields.Date.today(),
            'retention_percent': 10.0,
            'advance_payment_id': payment.id,
            'advance_recovery_type': 'percent',
            'advance_recovery_percent': 20.0,
            'invoice_line_ids': [
                (0, 0, {
                    'product_id': self.work_product.id,
                    'price_unit': 100000.0,
                    'quantity': 1.0,
                    'tax_ids': [(6, 0, self.tax_15.ids)],
                }),
            ],
        })
        invoice.action_post()
        self.assertTrue(invoice.retention_lines_created)

        # Reset to draft
        invoice.button_draft()
        self.assertFalse(invoice.retention_lines_created)
        self.assertFalse(invoice.line_ids.filtered(lambda l: l.cb_is_retention_line))
        self.assertFalse(invoice.line_ids.filtered(lambda l: l.cb_is_advance_recovery_line))

        # Re-post
        invoice.action_post()
        self.assertTrue(invoice.retention_lines_created)
        # Verify exactly one retention line exists (no duplication)
        self.assertEqual(len(invoice.line_ids.filtered(lambda l: l.cb_is_retention_line)), 1)
        self.assertEqual(len(invoice.line_ids.filtered(lambda l: l.cb_is_advance_recovery_line)), 1)

    def test_05_release_retention_wizard(self):
        """Verify Release Retention wizard moves retention from Retention Receivable to AR."""
        payment = self._create_advance_payment(200000.0)

        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'journal_id': self.sale_journal.id,
            'invoice_date': fields.Date.today(),
            'retention_percent': 10.0,
            'advance_payment_id': payment.id,
            'advance_recovery_type': 'percent',
            'advance_recovery_percent': 20.0,
            'invoice_line_ids': [
                (0, 0, {
                    'product_id': self.work_product.id,
                    'price_unit': 100000.0,
                    'quantity': 1.0,
                    'tax_ids': [(6, 0, self.tax_15.ids)],
                }),
            ],
        })
        invoice.action_post()

        # General journal for release
        general_journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company.id),
        ], limit=1)

        # Launch wizard
        wizard = self.env['cb.release.retention.wizard'].create({
            'partner_id': self.partner.id,
            'invoice_ids': [(6, 0, invoice.ids)],
            'journal_id': general_journal.id,
            'release_date': fields.Date.today(),
            'release_amount': 10000.0,
        })

        self.assertAlmostEqual(wizard.total_retention_held, 10000.0)
        self.assertAlmostEqual(wizard.retention_balance, 10000.0)

        res = wizard.action_release()
        release_entry = self.env['account.move'].browse(res['res_id'])
        self.assertEqual(release_entry.state, 'posted')

        # Check JE: DR AR 10,000, CR Retention 10,000
        ar_release = release_entry.line_ids.filtered(lambda l: l.account_id == self.partner.property_account_receivable_id)
        ret_release = release_entry.line_ids.filtered(lambda l: l.account_id == self.retention_account)

        self.assertAlmostEqual(ar_release.debit, 10000.0)
        self.assertAlmostEqual(ret_release.credit, 10000.0)

    def test_06_excel_report_generation(self):
        """Verify the Retention & Advance report wizard generates a valid Excel file."""
        wizard = self.env['cb.retention.report.wizard'].create({
            'partner_id': self.partner.id,
            'as_of_date': fields.Date.today(),
        })
        wizard.action_generate_excel()
        self.assertTrue(wizard.excel_file)
        self.assertTrue(wizard.excel_filename.endswith('.xlsx'))
