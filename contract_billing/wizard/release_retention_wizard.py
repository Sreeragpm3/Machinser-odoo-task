# -*- coding: utf-8 -*-
"""
Release Retention Wizard (Req 5)

Lets the accountant pick invoices with unreleased retention, enter an amount
and a date, and create a journal entry that moves the retained amount from
Retention Receivable → normal Accounts Receivable so the client can pay it.

Journal entry created:
    DR  Accounts Receivable (normal AR)    release_amount  (now collectable)
        CR  Retention Receivable               release_amount  (clears retention)

Posted journal entries are never deleted or edited — this wizard only creates
new entries (Req – rules).
"""

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare


class ReleaseRetentionWizard(models.TransientModel):
    _name = 'cb.release.retention.wizard'
    _description = 'Release Retention'

    # ------------------------------------------------------------------ #
    # Filter fields
    # ------------------------------------------------------------------ #
    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Customer',
        required=True,
    )

    sale_order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Project (Sales Order)',
        domain="[('partner_id', 'child_of', partner_id)]",
        help="Filter invoices by project. Leave blank to see all invoices for the customer.",
    )

    invoice_ids = fields.Many2many(
        comodel_name='account.move',
        string='Invoices',
        domain="[('partner_id', 'child_of', partner_id), "
               " ('move_type', '=', 'out_invoice'), "
               " ('state', '=', 'posted'), "
               " ('retention_percent', '>', 0)]",
        help="Select the invoices whose retention you want to release.",
    )

    # ------------------------------------------------------------------ #
    # Release parameters
    # ------------------------------------------------------------------ #
    total_retention_held = fields.Monetary(
        string='Total Retention Held',
        compute='_compute_totals',
        currency_field='currency_id',
    )

    total_retention_released = fields.Monetary(
        string='Already Released',
        compute='_compute_totals',
        currency_field='currency_id',
    )

    retention_balance = fields.Monetary(
        string='Retention Balance',
        compute='_compute_totals',
        currency_field='currency_id',
    )

    release_amount = fields.Monetary(
        string='Amount to Release',
        currency_field='currency_id',
        required=True,
        help="Full or partial release. Cannot exceed the retention balance.",
    )

    release_date = fields.Date(
        string='Release Date',
        required=True,
        default=fields.Date.context_today,
    )

    @api.model
    def _default_journal_id(self):
        return self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('code', '=', 'MISC'),
            ('company_id', '=', self.env.company.id),
        ], limit=1) or self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.env.company.id),
        ], limit=1)

    journal_id = fields.Many2one(
        comodel_name='account.journal',
        string='Journal',
        required=True,
        default=_default_journal_id,
        domain=[('type', '=', 'general')],
        help="Miscellaneous / general journal for the release entry.",
    )

    currency_id = fields.Many2one(
        comodel_name='res.currency',
        compute='_compute_currency',
    )

    company_id = fields.Many2one(
        comodel_name='res.company',
        default=lambda self: self.env.company,
    )

    # ------------------------------------------------------------------ #
    # Computed
    # ------------------------------------------------------------------ #
    @api.depends('company_id')
    def _compute_currency(self):
        for rec in self:
            rec.currency_id = rec.company_id.currency_id

    @api.depends('invoice_ids', 'partner_id')
    def _compute_totals(self):
        for rec in self:
            held = sum(rec.invoice_ids.mapped('retention_amount'))
            # Released = sum of credit on release lines on Retention Receivable linked to these invoices
            released_lines = self.env['account.move.line'].search([
                ('cb_release_invoice_ids', 'in', rec.invoice_ids.ids),
                ('cb_is_release_line', '=', True),
                ('move_id.state', '=', 'posted'),
            ])
            released = sum(
                abs(l.amount_currency) if l.currency_id == rec.currency_id else abs(l.balance)
                for l in released_lines
            )
            rec.total_retention_held = held
            rec.total_retention_released = released
            rec.retention_balance = max(held - released, 0.0)
            if not rec.release_amount:
                rec.release_amount = rec.retention_balance

    @api.onchange('partner_id', 'sale_order_id')
    def _onchange_filter_invoices(self):
        """Auto-populate invoice_ids when partner / project changes."""
        if not self.partner_id:
            self.invoice_ids = False
            return

        domain = [
            ('partner_id', 'child_of', self.partner_id.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('retention_percent', '>', 0),
        ]
        if self.sale_order_id:
            domain.append(
                ('invoice_line_ids.sale_line_ids.order_id', '=', self.sale_order_id.id)
            )
        self.invoice_ids = self.env['account.move'].search(domain)

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    @api.constrains('release_amount')
    def _check_release_amount(self):
        for rec in self:
            if float_compare(
                rec.release_amount, rec.retention_balance,
                precision_rounding=rec.currency_id.rounding,
            ) > 0:
                raise ValidationError(_(
                    "Release amount (%(rel)s) exceeds retention balance (%(bal)s).",
                    rel=rec.currency_id.format(rec.release_amount),
                    bal=rec.currency_id.format(rec.retention_balance),
                ))

    # ------------------------------------------------------------------ #
    # Action: create the release journal entry (Req 5)
    # ------------------------------------------------------------------ #
    def action_release(self):
        self.ensure_one()

        if not self.invoice_ids:
            raise UserError(_("Please select at least one invoice."))

        if self.currency_id.is_zero(self.release_amount):
            raise UserError(_("Please enter an amount to release greater than zero."))

        retention_account = self.company_id.retention_receivable_account_id
        if not retention_account:
            raise UserError(_(
                "Please configure the Retention Receivable Account in "
                "Accounting → Configuration → Settings."
            ))

        ar_account = self.partner_id.property_account_receivable_id
        if not ar_account:
            raise UserError(_(
                "The customer has no Accounts Receivable account configured."
            ))

        release_amount_company = self.currency_id._convert(
            self.release_amount, self.company_id.currency_id, self.company_id, self.release_date
        )

        # ---------------------------------------------------------------- #
        # Journal entry:
        #   DR  Accounts Receivable (normal)   release_amount (now due)
        #       CR  Retention Receivable       release_amount (clears retention)
        # ---------------------------------------------------------------- #
        move_vals = {
            'move_type': 'entry',
            'journal_id': self.journal_id.id,
            'date': self.release_date,
            'ref': _('Retention Release – %s') % ', '.join(self.invoice_ids.mapped('name')),
            'company_id': self.company_id.id,
            'line_ids': [
                # DR Accounts Receivable (now due like normal AR)
                (0, 0, {
                    'name': _('Release Retention – %s') % self.partner_id.name,
                    'account_id': ar_account.id,
                    'partner_id': self.partner_id.id,
                    'debit': release_amount_company,
                    'credit': 0.0,
                    'amount_currency': self.release_amount,
                    'currency_id': self.currency_id.id,
                    'date_maturity': self.release_date,
                }),
                # CR Retention Receivable (clears the held balance)
                (0, 0, {
                    'name': _('Release Retention – %s') % self.partner_id.name,
                    'account_id': retention_account.id,
                    'partner_id': self.partner_id.id,
                    'debit': 0.0,
                    'credit': release_amount_company,
                    'amount_currency': -self.release_amount,
                    'currency_id': self.currency_id.id,
                    'date_maturity': self.release_date,
                    'cb_is_release_line': True,
                    'cb_release_invoice_ids': [(6, 0, self.invoice_ids.ids)],
                }),
            ],
        }

        release_move = self.env['account.move'].create(move_vals)
        release_move._post()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Retention Release Entry'),
            'res_model': 'account.move',
            'res_id': release_move.id,
            'view_mode': 'form',
            'target': 'current',
        }
