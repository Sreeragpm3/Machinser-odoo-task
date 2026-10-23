# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    """
    Surfaces res.company retention fields in the standard Accounting →
    Configuration → Settings page.  Uses the standard Odoo pattern:
    `related` field pointing at the company record.  No extra storage needed.
    (Req 1)
    """
    _inherit = 'res.config.settings'

    retention_receivable_account_id = fields.Many2one(
        comodel_name='account.account',
        related='company_id.retention_receivable_account_id',
        string='Retention Receivable Account',
        readonly=False,
        domain=[('account_type', 'like', 'asset_receivable')],
        help="Account to hold withheld retention amounts until project handover.",
    )

    retention_default_percent = fields.Float(
        related='company_id.retention_default_percent',
        string='Default Retention %',
        readonly=False,
        help="Applied automatically to new customer invoices; editable per invoice.",
    )
