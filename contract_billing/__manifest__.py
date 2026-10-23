# -*- coding: utf-8 -*-
{
    'name': 'Contract Billing – Retention & Advance',
    'version': '19.0.1.0.0',
    'summary': 'Retention deductions and advance recovery on customer invoices for contracting companies',
    'description': """
        Handles the full lifecycle of retention and advance recovery for contracting companies:
        - Retention % deducted from each progress invoice, held in a separate Retention Receivable account
        - Advance recovery: auto-reconcile advance payments against invoices
        - Release Retention wizard at project handover
        - Excel report per customer/project
        - Printed invoice shows deductions and amount due now
    """,
    'category': 'Accounting/Accounting',
    'author': 'Sreerag P Murali',
    'depends': ['account', 'sale_management'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/account_move_views.xml',
        'views/release_retention_wizard_views.xml',
        'views/retention_report_wizard_views.xml',
        'report/invoice_report_templates.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
