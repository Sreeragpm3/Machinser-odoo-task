# -*- coding: utf-8 -*-
from odoo import fields, models, _

class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    cb_is_retention_line = fields.Boolean(
        string='Is Retention Line',
        default=False,
        copy=False,
        help="Set True on the retention receivable line injected by contract_billing.",
    )

    cb_is_advance_recovery_line = fields.Boolean(
        string='Is Advance Recovery Line',
        default=False,
        copy=False,
        help="Set True on the advance recovery line injected by contract_billing.",
    )

    cb_is_release_line = fields.Boolean(
        string='Is Retention Release Line',
        default=False,
        copy=False,
        help="Set True on the debit line of a retention release journal entry.",
    )

    cb_release_invoice_ids = fields.Many2many(
        comodel_name='account.move',
        relation='cb_release_line_invoice_rel',
        column1='line_id',
        column2='invoice_id',
        string='Released For Invoices',
        copy=False,
        help="Invoices whose retention this release line covers.",
    )
