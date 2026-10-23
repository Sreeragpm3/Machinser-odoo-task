# -*- coding: utf-8 -*-
"""
Retention & Advance Report Wizard (Req 8)

Generates a per-customer/project Excel report showing:
  Total Invoiced | Retention Held | Released | Balance |
  Advance Received | Recovered | Advance Balance

Uses xlsxwriter (bundled with Odoo) — no extra dependencies.
"""

import io
import base64
from datetime import date

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class RetentionReportWizard(models.TransientModel):
    _name = 'cb.retention.report.wizard'
    _description = 'Retention & Advance Report'

    # ------------------------------------------------------------------ #
    # Filter parameters
    # ------------------------------------------------------------------ #
    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Customer',
        domain=[('customer_rank', '>', 0)],
        help="Leave blank to report all customers.",
    )

    sale_order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Project (Sales Order)',
        domain="[('partner_id', 'child_of', partner_id)]" if 'partner_id' else "[]",
        help="Leave blank for all projects.",
    )

    as_of_date = fields.Date(
        string='As of Date',
        required=True,
        default=fields.Date.context_today,
    )

    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        default=lambda self: self.env.company,
    )

    # Output (set after generation)
    excel_file = fields.Binary(string='Excel Report', readonly=True)
    excel_filename = fields.Char(string='Filename', readonly=True)

    # ------------------------------------------------------------------ #
    # Data computation
    # ------------------------------------------------------------------ #
    def _get_report_data(self):
        """
        Return a list of dicts, one per (partner, sale_order) combination.
        """
        domain = [
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
            ('invoice_date', '<=', self.as_of_date),
            ('company_id', '=', self.company_id.id),
            ('retention_percent', '>', 0),
        ]
        if self.partner_id:
            domain.append(('partner_id', 'child_of', self.partner_id.id))
        if self.sale_order_id:
            domain.append(
                ('invoice_line_ids.sale_line_ids.order_id', '=', self.sale_order_id.id)
            )

        invoices = self.env['account.move'].search(domain)

        # Group by (partner, sale_order)
        groups = {}
        for inv in invoices:
            so = inv._get_project_sale_order()
            key = (inv.partner_id.id, so.id if so else False)
            if key not in groups:
                groups[key] = {
                    'partner': inv.partner_id,
                    'sale_order': so,
                    'invoices': self.env['account.move'],
                }
            groups[key]['invoices'] |= inv

        rows = []
        for key, grp in groups.items():
            partner = grp['partner']
            so = grp['sale_order']
            invs = grp['invoices']

            # Sign: out_invoice = +1, out_refund = -1
            total_invoiced = sum(
                (inv.amount_untaxed if inv.move_type == 'out_invoice' else -inv.amount_untaxed)
                for inv in invs
            )
            retention_held = sum(
                (inv.retention_amount if inv.move_type == 'out_invoice' else -inv.retention_amount)
                for inv in invs
            )

            # Released = credit on release lines linked to these invoices
            released_lines = self.env['account.move.line'].search([
                ('cb_release_invoice_ids', 'in', invs.ids),
                ('cb_is_release_line', '=', True),
                ('move_id.state', '=', 'posted'),
                ('move_id.date', '<=', self.as_of_date),
            ])
            retention_released = sum(abs(l.amount_currency) or abs(l.balance) for l in released_lines)
            retention_balance = max(retention_held - retention_released, 0.0)

            # Advance figures (per payment, since one payment per invoice)
            advance_received = 0.0
            advance_recovered = 0.0
            seen_payments = set()
            for inv in invs:
                if inv.advance_payment_id and inv.advance_payment_id.id not in seen_payments:
                    seen_payments.add(inv.advance_payment_id.id)
                    advance_received += inv.advance_payment_id.amount
                sign = 1 if inv.move_type == 'out_invoice' else -1
                advance_recovered += sign * (inv.advance_recovery_amount or 0.0)

            advance_balance = advance_received - advance_recovered

            rows.append({
                'partner_name': partner.display_name,
                'project': so.name if so else _('(No Project)'),
                'total_invoiced': total_invoiced,
                'retention_held': retention_held,
                'retention_released': retention_released,
                'retention_balance': retention_balance,
                'advance_received': advance_received,
                'advance_recovered': advance_recovered,
                'advance_balance': advance_balance,
            })

        return rows

    # ------------------------------------------------------------------ #
    # Excel generation (Req 8)
    # ------------------------------------------------------------------ #
    def action_generate_excel(self):
        self.ensure_one()
        try:
            import xlsxwriter
        except ImportError:
            raise UserError(_("xlsxwriter is not installed. Please install it."))

        rows = self._get_report_data()

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        ws = workbook.add_worksheet(_('Retention & Advance'))

        # ---- Formats ----
        bold = workbook.add_format({'bold': True, 'bg_color': '#1F3864', 'font_color': '#FFFFFF', 'border': 1})
        money = workbook.add_format({'num_format': '#,##0.00', 'border': 1})
        text = workbook.add_format({'border': 1})
        title_fmt = workbook.add_format({'bold': True, 'font_size': 14})
        sub_fmt = workbook.add_format({'italic': True, 'font_color': '#555555'})

        # ---- Title ----
        ws.write(0, 0, _('Retention & Advance Report'), title_fmt)
        ws.write(1, 0, _('As of: %s | Company: %s') % (
            self.as_of_date.strftime('%d/%m/%Y'),
            self.company_id.name,
        ), sub_fmt)

        # ---- Headers ----
        headers = [
            _('Customer'), _('Project'),
            _('Total Invoiced'),
            _('Retention Held'), _('Retention Released'), _('Retention Balance'),
            _('Advance Received'), _('Advance Recovered'), _('Advance Balance'),
        ]
        ws.set_column(0, 0, 30)
        ws.set_column(1, 1, 28)
        ws.set_column(2, 8, 20)

        for col, header in enumerate(headers):
            ws.write(3, col, header, bold)

        # ---- Data ----
        if rows:
            for row_idx, row in enumerate(rows, start=4):
                ws.write(row_idx, 0, row['partner_name'], text)
                ws.write(row_idx, 1, row['project'], text)
                ws.write(row_idx, 2, row['total_invoiced'], money)
                ws.write(row_idx, 3, row['retention_held'], money)
                ws.write(row_idx, 4, row['retention_released'], money)
                ws.write(row_idx, 5, row['retention_balance'], money)
                ws.write(row_idx, 6, row['advance_received'], money)
                ws.write(row_idx, 7, row['advance_recovered'], money)
                ws.write(row_idx, 8, row['advance_balance'], money)

            # ---- Totals row ----
            total_row = len(rows) + 4
            total_fmt = workbook.add_format({'bold': True, 'num_format': '#,##0.00', 'border': 1, 'bg_color': '#D9E1F2'})
            ws.write(total_row, 0, _('TOTAL'), workbook.add_format({'bold': True, 'border': 1, 'bg_color': '#D9E1F2'}))
            ws.write(total_row, 1, '', workbook.add_format({'border': 1, 'bg_color': '#D9E1F2'}))
            for col_idx, key in enumerate([
                'total_invoiced', 'retention_held', 'retention_released', 'retention_balance',
                'advance_received', 'advance_recovered', 'advance_balance',
            ]):
                ws.write(total_row, col_idx + 2, sum(r[key] for r in rows), total_fmt)
        else:
            ws.write(4, 0, _('No transactions found matching the selected filters.'), text)

        workbook.close()
        output.seek(0)

        filename = 'Retention_Advance_%s.xlsx' % date.today().strftime('%Y%m%d')
        self.write({
            'excel_file': base64.b64encode(output.read()),
            'excel_filename': filename,
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
