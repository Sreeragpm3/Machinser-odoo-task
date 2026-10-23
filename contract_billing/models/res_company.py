# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    """
    Extend res.company to store company-wide defaults for the
    retention & advance module.  Each company in a multi-company
    database has its own settings (Req 10 – multi-company).
    """
    _inherit = 'res.company'

    retention_receivable_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Retention Receivable Account',
        company_dependent=True,
        domain=[('account_type', 'like', 'asset_receivable')],
        help=(
            "Dedicated receivable account that holds withheld retention amounts "
            "until the project is handed over and the retention is released. "
            "Must be a reconcilable account."
        ),
    )

    retention_default_percent = fields.Float(
        string='Default Retention %',
        default=10.0,
        help="Default retention percentage applied to new customer invoices. "
             "Can be overridden on each individual invoice.",
    )
