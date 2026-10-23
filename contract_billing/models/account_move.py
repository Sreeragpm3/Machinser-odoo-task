# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_round


class AccountMove(models.Model):
    _inherit = 'account.move'

    retention_percent = fields.Float(
        string='Retention %',
        digits=(5, 2),
        help="Percentage of the untaxed amount to withhold as retention guarantee.",
    )

    retention_amount = fields.Monetary(
        string='Retention Amount',
        compute='_compute_retention_recovery',
        store=True,
        currency_field='currency_id',
        help="Computed: Untaxed Amount × Retention %",
    )

    retention_due_date = fields.Date(
        string='Retention Due Date',
        help="Expected project handover date. "
             "The retention receivable line will carry this as its due date.",
    )

    advance_payment_id = fields.Many2one(
        comodel_name='account.payment',
        string='Advance Payment',
        domain="[('partner_id', '=', partner_id), "
               " ('payment_type', '=', 'inbound'), "
               " ('state', 'in', ('in_process', 'paid')), "
               " ('is_reconciled', '=', False)]",
        help="Customer advance payment to recover from this invoice.",
    )

    advance_recovery_type = fields.Selection(
        selection=[('percent', 'Percentage'), ('fixed', 'Fixed Amount')],
        string='Recovery Type',
        default='percent',
    )

    advance_recovery_percent = fields.Float(
        string='Recovery %',
        digits=(5, 2),
        help="Percentage of the untaxed amount to recover from the advance.",
    )

    advance_recovery_amount = fields.Monetary(
        string='Advance Recovery Amount',
        compute='_compute_retention_recovery',
        store=True,
        readonly=False,   # allow manual override when type = 'fixed'
        currency_field='currency_id',
        help="Amount recovered from the advance payment on this invoice. "
             "Capped at the remaining advance balance.",
    )

    advance_remaining = fields.Monetary(
        string='Advance Remaining After This Invoice',
        compute='_compute_retention_recovery',
        store=True,
        currency_field='currency_id',
        help="Remaining advance balance after deducting this invoice's recovery.",
    )

    amount_to_pay_now = fields.Monetary(
        string='Amount Due Now',
        compute='_compute_retention_recovery',
        store=True,
        currency_field='currency_id',
        help="Invoice total − Retention − Advance Recovery = Amount client pays now.",
    )

    retention_lines_created = fields.Boolean(
        string='Retention Lines Created',
        copy=False,
        help="Internal flag: True once _post() has injected retention/advance lines. "
             "Prevents duplication if reset to draft and re-posted.",
    )

    def _get_advance_balance(self, payment):
        """
        Return the remaining balance on `payment` in the invoice's currency,
        excluding any recovery already matched by THIS invoice (so editing
        the invoice doesn't show the balance as already consumed by itself).
        """
        if not payment:
            return 0.0

        total_balance = 0.0

        # If payment has journal entry lines, check their unreconciled credit
        if payment.move_id and payment.move_id.line_ids:
            credit_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.reconcile and not l.reconciled and (l.credit > 0 or l.balance < 0)
            )
            if not credit_lines:
                credit_lines = payment.move_id.line_ids.filtered(
                    lambda l: not l.reconciled and (l.credit > 0 or l.balance < 0)
                )

            for line in credit_lines:
                if line.currency_id == self.currency_id:
                    total_balance += abs(line.amount_residual_currency)
                else:
                    company_residual = abs(line.amount_residual)
                    total_balance += self.company_id.currency_id._convert(
                        company_residual, self.currency_id, self.company_id, self.date or fields.Date.context_today(self)
                    )
        else:
            # Fallback when payment has no separate lines yet
            other_invoices = self.env['account.move'].search([
                ('advance_payment_id', '=', payment.id),
                ('state', '=', 'posted'),
                ('id', '!=', self.id or 0),
            ])
            recovered = sum(other_invoices.mapped('advance_recovery_amount'))
            total_balance = max(payment.amount - recovered, 0.0)

        # Add back amount already recovered by THIS invoice (if in edit mode)
        if self.id and self.retention_lines_created:
            own_adv_lines = self.line_ids.filtered(lambda l: l.cb_is_advance_recovery_line)
            for own_line in own_adv_lines:
                if own_line.currency_id == self.currency_id:
                    total_balance += abs(own_line.amount_currency)
                else:
                    total_balance += self.company_id.currency_id._convert(
                        abs(own_line.balance), self.currency_id, self.company_id, self.date or fields.Date.context_today(self)
                    )

        return total_balance

    @api.depends(
        'amount_untaxed', 'amount_total',
        'retention_percent',
        'advance_payment_id', 'advance_recovery_type',
        'advance_recovery_percent', 'advance_recovery_amount',
    )
    def _compute_retention_recovery(self):
        for move in self:
            if move.move_type not in ('out_invoice', 'out_refund'):
                move.retention_amount = 0.0
                move.advance_recovery_amount = move.advance_recovery_amount or 0.0
                move.advance_remaining = 0.0
                move.amount_to_pay_now = move.amount_total
                continue

            # Retention
            retention = float_round(
                abs(move.amount_untaxed) * (move.retention_percent / 100.0),
                precision_rounding=move.currency_id.rounding,
            )
            move.retention_amount = retention

            # Advance recovery
            adv_balance = move._get_advance_balance(move.advance_payment_id) if move.advance_payment_id else 0.0
            if move.advance_recovery_type == 'percent':
                recovery = float_round(
                    abs(move.amount_untaxed) * (move.advance_recovery_percent / 100.0),
                    precision_rounding=move.currency_id.rounding,
                )
            else:
                recovery = move.advance_recovery_amount or 0.0

            # Cap at available advance balance
            recovery = min(recovery, adv_balance)
            move.advance_recovery_amount = recovery
            move.advance_remaining = max(adv_balance - recovery, 0.0)

            # Amount due now = total (incl VAT) − retention − recovery
            move.amount_to_pay_now = max(abs(move.amount_total) - retention - recovery, 0.0)

    @api.onchange('partner_id', 'company_id')
    def _onchange_set_default_retention(self):
        """Populate retention_percent from company settings when partner changes."""
        for move in self:
            if move.move_type in ('out_invoice', 'out_refund') and not move.retention_percent:
                move.retention_percent = move.company_id.retention_default_percent

    @api.constrains('advance_recovery_amount', 'advance_payment_id')
    def _check_advance_recovery_cap(self):
        for move in self:
            if not move.advance_payment_id:
                continue
            adv_balance = move._get_advance_balance(move.advance_payment_id)
            if float_compare(
                move.advance_recovery_amount,
                adv_balance,
                precision_rounding=move.currency_id.rounding,
            ) > 0:
                raise ValidationError(_(
                    "The advance recovery amount (%(recovery)s) exceeds the "
                    "remaining advance balance (%(balance)s) on payment %(payment)s.",
                    recovery=move.currency_id.format(move.advance_recovery_amount),
                    balance=move.currency_id.format(adv_balance),
                    payment=move.advance_payment_id.name,
                ))

    def _get_project_sale_order(self):
        """Return the first sale.order linked to this invoice, or False."""
        self.ensure_one()
        orders = self.invoice_line_ids.sale_line_ids.order_id
        return orders[0] if orders else False

    def _post(self, soft=True):
        """
        Split the receivable line BEFORE standard posting (while still draft,
        avoiding readonly line_ids error on posted moves), then post and reconcile.
        """
        for move in self.filtered(
            lambda m: m.move_type in ('out_invoice', 'out_refund')
                      and not m.retention_lines_created
                      and (m.retention_percent or m.advance_payment_id)
        ):
            move._apply_retention_and_advance()

        posted = super()._post(soft=soft)

        for move in posted.filtered(lambda m: m.retention_lines_created):
            move._reconcile_advance_recovery()

        return posted

    def _apply_retention_and_advance(self):
        """
        Split the AR receivable line of a posted invoice into:
          1. Normal AR line  (amount_to_pay_now)
          2. Retention Receivable line  (retention_amount, due = retention_due_date)
          3. Advance account line  (advance_recovery_amount, to be reconciled)

        Then reconcile (3) against the advance payment.
        Idempotency: guarded by retention_lines_created flag.
        """
        self.ensure_one()

        retention_account = self.company_id.retention_receivable_account_id
        if not retention_account and self.retention_percent:
            raise UserError(_(
                "Please configure the Retention Receivable Account in "
                "Accounting → Configuration → Settings before posting "
                "an invoice with retention."
            ))

        currency = self.currency_id
        sign = 1 if self.move_type == 'out_invoice' else -1

        retention_amt = self.retention_amount
        recovery_amt = self.advance_recovery_amount

        # Nothing to do — no deductions configured
        if currency.is_zero(retention_amt) and currency.is_zero(recovery_amt):
            return

        ar_line = self.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
                      and not l.cb_is_retention_line
                      and not l.cb_is_advance_recovery_line
        )
        if not ar_line:
            return

        ar_line = ar_line[0]

        # Multi-currency conversions for company currency debit/credit
        retention_amt_company = currency._convert(
            retention_amt, self.company_id.currency_id, self.company_id, self.date or fields.Date.context_today(self)
        )
        recovery_amt_company = currency._convert(
            recovery_amt, self.company_id.currency_id, self.company_id, self.date or fields.Date.context_today(self)
        )
        total_deduction_curr = retention_amt + recovery_amt
        total_deduction_comp = retention_amt_company + recovery_amt_company

        if sign == 1:
            new_debit = max(ar_line.debit - total_deduction_comp, 0.0)
            new_credit = ar_line.credit
            new_amount_curr = ar_line.amount_currency - total_deduction_curr
        else:
            new_debit = ar_line.debit
            new_credit = max(ar_line.credit - total_deduction_comp, 0.0)
            new_amount_curr = ar_line.amount_currency + total_deduction_curr

        ar_line.with_context(check_move_validity=False).write({
            'debit': new_debit,
            'credit': new_credit,
            'amount_currency': new_amount_curr,
        })

        new_lines_vals = []

        if not currency.is_zero(retention_amt) and retention_account:
            new_lines_vals.append({
                'move_id': self.id,
                'name': _('Retention – %s%%') % self.retention_percent,
                'account_id': retention_account.id,
                'partner_id': self.partner_id.id,
                'currency_id': currency.id,
                'debit': sign * retention_amt_company if sign == 1 else 0.0,
                'credit': -sign * retention_amt_company if sign == -1 else 0.0,
                'amount_currency': sign * retention_amt,
                'date_maturity': self.retention_due_date or ar_line.date_maturity,
                'display_type': 'payment_term',
                'cb_is_retention_line': True,
            })

        adv_account = None
        payment_credit_lines = self.env['account.move.line']
        if not currency.is_zero(recovery_amt) and self.advance_payment_id:
            payment = self.advance_payment_id
            if not payment.move_id:
                if not payment.outstanding_account_id:
                    payment.outstanding_account_id = payment.journal_id.default_account_id.id
                payment._generate_journal_entry()
                payment.move_id.filtered(lambda m: m.state == 'draft').action_post()

            payment_credit_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.reconcile and not l.reconciled and (l.credit > 0 or l.balance < 0)
            )
            if not payment_credit_lines:
                payment_credit_lines = payment.move_id.line_ids.filtered(
                    lambda l: not l.reconciled and (l.credit > 0 or l.balance < 0)
                )
            if payment_credit_lines:
                adv_account = payment_credit_lines[0].account_id
            else:
                adv_account = payment.destination_account_id or self.partner_id.property_account_receivable_id

        if adv_account and not currency.is_zero(recovery_amt):
            new_lines_vals.append({
                'move_id': self.id,
                'name': _('Advance Recovery – %s') % self.advance_payment_id.name,
                'account_id': adv_account.id,
                'partner_id': self.partner_id.id,
                'currency_id': currency.id,
                'debit': sign * recovery_amt_company if sign == 1 else 0.0,
                'credit': -sign * recovery_amt_company if sign == -1 else 0.0,
                'amount_currency': sign * recovery_amt,
                'display_type': 'payment_term',
                'cb_is_advance_recovery_line': True,
            })

        if new_lines_vals:
            self.with_context(
                check_move_validity=False,
                skip_is_manually_modified=True,
            ).write({'line_ids': [(0, 0, vals) for vals in new_lines_vals]})

        self.retention_lines_created = True

    def _reconcile_advance_recovery(self):
        """Reconcile the advance recovery line with the payment after posting."""
        self.ensure_one()
        if not self.advance_payment_id:
            return

        our_adv_line = self.line_ids.filtered(
            lambda l: l.cb_is_advance_recovery_line and not l.reconciled
        )
        if not our_adv_line:
            return

        payment = self.advance_payment_id
        payment_credit_lines = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.reconcile and not l.reconciled and (l.credit > 0 or l.balance < 0)
        )
        if our_adv_line and payment_credit_lines:
            (our_adv_line + payment_credit_lines).reconcile()

    def button_draft(self):
        """
        Before resetting to draft, undo the retention/advance injected lines:
          - Unreconcile the advance recovery partial reconcile
          - Remove the injected move lines (allowed on draft moves)
          - Restore the original AR line amounts
          - Reset the idempotency flag
        No posted entry is ever deleted or edited directly (Req – rules).
        """
        for move in self.filtered(lambda m: m.retention_lines_created):
            move._undo_retention_and_advance()
        return super().button_draft()

    def _undo_retention_and_advance(self):
        """Reverse the work done by _apply_retention_and_advance()."""
        self.ensure_one()

        adv_line = self.line_ids.filtered(lambda l: l.cb_is_advance_recovery_line)
        retention_line = self.line_ids.filtered(lambda l: l.cb_is_retention_line)

        # 1. Unreconcile advance recovery lines
        for line in adv_line:
            (line.matched_debit_ids + line.matched_credit_ids).unlink()

        # 2. Restore the original AR line balance
        ar_line = self.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
                      and not l.cb_is_retention_line
                      and not l.cb_is_advance_recovery_line
        )
        if ar_line:
            total_debit_to_restore = sum(retention_line.mapped('debit')) + sum(adv_line.mapped('debit'))
            total_credit_to_restore = sum(retention_line.mapped('credit')) + sum(adv_line.mapped('credit'))
            total_curr_to_restore = sum(retention_line.mapped('amount_currency')) + sum(adv_line.mapped('amount_currency'))

            ar_line[0].with_context(check_move_validity=False).write({
                'debit': ar_line[0].debit + total_debit_to_restore,
                'credit': ar_line[0].credit + total_credit_to_restore,
                'amount_currency': ar_line[0].amount_currency + total_curr_to_restore,
            })

        # 3. Remove injected lines
        lines_to_remove = retention_line + adv_line
        lines_to_remove.with_context(check_move_validity=False, force_delete=True, dynamic_unlink=True).unlink()

        # 4. Reset flag
        self.retention_lines_created = False

    def _reverse_move_vals(self, default_values, cancel=False):
        """
        When a credit note is created from an invoice that has retention
        or advance fields, copy those fields proportionally to the reversal.
        The same _post() override then handles the reversed entries.
        """
        vals = super()._reverse_move_vals(default_values, cancel=cancel)

        if self.move_type == 'out_invoice' and (
            self.retention_percent or self.advance_payment_id
        ):
            vals.update({
                'retention_percent': self.retention_percent,
                'retention_due_date': self.retention_due_date,
                'advance_payment_id': self.advance_payment_id.id if self.advance_payment_id else False,
                'advance_recovery_type': self.advance_recovery_type,
                'advance_recovery_percent': self.advance_recovery_percent,
            })

        return vals



