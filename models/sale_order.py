# Copyright 2014-2019 Akretion (http://www.akretion.com)
# @author Alexis de Lattre <alexis.delattre@akretion.com>
# Copyright 2016-2019 Sodexis (http://sodexis.com)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare
from dateutil.relativedelta import relativedelta
import odoo.addons.decimal_precision as dp
import logging


logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    rental_guarantee_price_total =  fields.Monetary(string='Total Rental Guarantee', store=True, readonly=True, tracking=True, compute='_compute_total')
    num_word = fields.Char(string="Amount In Words:", compute='_compute_amount_in_word')

    def action_cancel(self):
        """
            In case cancelling a SO which sells rental product.
            Picking in should be created manually
        """
        res = super(SaleOrder, self).action_cancel()
        for order in self:
            for line in order.order_line.filtered(
                    lambda l: l.rental_type == 'rental_extension' and
                    l.extension_rental_id):
                initial_end_date = line.extension_rental_id.end_date
                line.extension_rental_id.in_move_id.write({
                    'date_expected': initial_end_date,
                    'date': initial_end_date,
                    })
        return res

    @api.depends('order_line.rental_qty','order_line.rental_guarantee_price')
    def _compute_total(self):
        guarantee_total = 0.00
        """
        check if any line of sale_order_line are 'new_rental'
        """
        if any(line['rental_type'] == 'new_rental' for line in self.order_line):
            for line in self.order_line:
                guarantee_total += line.rental_guarantee_price * line.rental_qty
            self.rental_guarantee_price_total = guarantee_total
        else:
            self.rental_guarantee_price_total = 0

        print(self.rental_guarantee_price_total)

    def _compute_amount_in_word(self):
        for rec in self:
            rec.num_word = str(rec.currency_id.amount_to_text(rec.amount_total))

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    rental = fields.Boolean(string='Rental', default=False)
    can_sell_rental = fields.Boolean(string='Can Sell from Rental')
    rental_type = fields.Selection([
        ('new_rental', 'New Rental'),
        ('rental_extension', 'Rental Extension')], 'Rental Type',
        readonly=True, states={'draft': [('readonly', False)]})
    extension_rental_id = fields.Many2one(
        'sale.rental', string='Rental to Extend')
    rental_qty = fields.Float(
        string='Rental Quantity',
        digits=dp.get_precision('Product Unit of Measure'),
        help="Indicate the number of items that will be rented.")
    sell_rental_id = fields.Many2one(
        'sale.rental', string='Rental to Sell')
    rental_guarantee_price =  fields.Monetary(string='GTEE Price', related='product_id.rental_guarantee_price', store=True)
    rental_guarantee_price_total =  fields.Monetary(string='Total GTEE', store=True)

    _sql_constraints = [(
        'rental_qty_positive',
        'CHECK(rental_qty >= 0)',
        'The rental quantity must be positive or null.'
        )]

    @api.constrains(
        'rental_type', 'extension_rental_id', 'start_date', 'end_date',
        'rental_qty', 'product_uom_qty', 'product_id', 'rental_guarantee_price')
    def _check_sale_line_rental(self):
        for line in self:
            if line.rental_type == 'rental_extension':
                if not line.extension_rental_id:
                    raise ValidationError(_(
                        "Missing 'Rental to Extend' on the sale order line "
                        "with rental service %s") % line.product_id.name)

                if line.rental_qty != line.extension_rental_id.rental_qty:
                    raise ValidationError(_(
                        "On the sale order line with rental service %s, "
                        "you are trying to extend a rental with a rental "
                        "quantity (%s) that is different from the quantity "
                        "of the original rental (%s). This is not supported.")
                        % (
                        line.product_id.name,
                        line.rental_qty,
                        line.extension_rental_id.rental_qty,))
            if line.rental_type in ('new_rental', 'rental_extension'):
                if not line.product_id.rented_product_id:
                    raise ValidationError(_(
                        "On the 'new rental' sale order line with product "
                        "'%s', we should have a rental service product !") % (
                        line.product_id.name))
                if line.product_uom_qty !=\
                        line.rental_qty * line.number_of_days:
                    raise ValidationError(_(
                        "On the sale order line with product '%s' "
                        "the Product Quantity (%s) should be the "
                        "number of days (%s) "
                        "multiplied by the Rental Quantity (%s).") % (
                        line.product_id.name, line.product_uom_qty,
                        line.number_of_days, line.rental_qty))
                # the module sale_start_end_dates checks that, when we have
                # must_have_dates, we have start + end dates
            elif line.sell_rental_id and line.product_uom_qty != line.sell_rental_id.rental_qty:
                if line.product_uom_qty != line.sell_rental_id.rental_qty:
                    raise ValidationError(_(
                        "On the sale order line with product %s "
                        "you are trying to sell a rented product with a "
                        "quantity (%s) that is different from the rented "
                        "quantity (%s). This is not supported.") % (
                        line.product_id.name,
                        line.product_uom_qty,
                        line.sell_rental_id.rental_qty))

    def _prepare_rental(self):
        self.ensure_one()
        return {'start_order_line_id': self.id}

    def _prepare_invoice_line(self):
        """
        Prepare the dict of values to create the new invoice line for a sales order line.

        :param qty: float quantity to invoice
        """
        self.ensure_one()
        res = super()._prepare_invoice_line()
        if self.start_date and self.end_date:
            res.update({"rental_qty": self.rental_qty})
        return res

    def _prepare_new_rental_procurement_values(self, group=False):
        vals = {
            'group_id': group,
            'sale_line_id': self.id,
            'date_planned': self.start_date,
            'route_ids': self.order_id.warehouse_id.rental_route_id,
            'warehouse_id': self.order_id.warehouse_id or False,
            'partner_id': self.order_id.partner_shipping_id.id,
            'company_id': self.order_id.company_id,
            }
        return vals

    def _action_launch_stock_rule(self):
        errors = []
        procurements = []
        for line in self:
            if (line.rental_type == 'new_rental' and line.product_id.rented_product_id):

                # create procurement group
                group = line.order_id.procurement_group_id
                if not group:
                    group = self.env['procurement.group'].create({
                        'name': line.order_id.name,
                        'move_type': line.order_id.picking_policy,
                        'sale_id': line.order_id.id,
                        'partner_id': line.order_id.partner_shipping_id.id,
                    })
                    line.order_id.procurement_group_id = group

                vals = line._prepare_new_rental_procurement_values(group)
                try:
                    self.env['procurement.group'].run([self.env['procurement.group'].Procurement(
                        line.product_id.rented_product_id, line.rental_qty,
                        line.product_id.rented_product_id.uom_id,
                        line.order_id.warehouse_id.rental_out_location_id,
                        line.name, line.order_id.name,line.order_id.company_id, vals)])
                except UserError as error:
                    errors.append(error.name)

                # create sale rental
                self.env['sale.rental'].create(line._prepare_rental())

            elif (
                    line.rental_type == 'rental_extension' and
                    line.product_id.rented_product_id and
                    line.extension_rental_id and
                    line.extension_rental_id.in_move_id):
                end_datetime = fields.Datetime.to_datetime(
                    line.end_date)
                line.extension_rental_id.in_move_id.write({
                    'date_expected': end_datetime,
                    'date': end_datetime,
                    })
            elif line.sell_rental_id:
                if line.sell_rental_id.out_move_id.state != 'done':
                    raise UserError(_(
                        'Cannot sell the rental %s because it has '
                        'not been delivered')
                        % line.sell_rental_id.display_name)
                line.sell_rental_id.in_move_id._action_cancel()

        if errors:
            raise UserError('\n'.join(errors))

        # call super() at the end, to make procurement_jit work
        res = super(SaleOrderLine, self)._action_launch_stock_rule()
        return res

    def _prepare_procurement_values(self, group_id=False):
        """
            Overriding this function to change the route
            on selling rental product
        """
        vals = super(SaleOrderLine, self)._prepare_procurement_values(group_id)
        if self.sell_rental_id:
            vals.update({
                'route_ids':
                    self.order_id.warehouse_id.sell_rented_product_route_id})
        return vals

    @api.onchange('product_id', 'rental_qty','rental_guarantee_price')
    def rental_product_id_change(self):
        res = {}
        if self.product_id:
            if self.product_id.rented_product_id:
                self.rental = True
                self.can_sell_rental = False
                self.sell_rental_id = False
                if not self.rental_type:
                    self.rental_type = 'new_rental'
                elif (
                        self.rental_type == 'new_rental' and
                        self.rental_qty and self.order_id.warehouse_id):
                    product_uom = self.product_id.rented_product_id.uom_id
                    warehouse = self.order_id.warehouse_id
                    rental_in_location = warehouse.rental_in_location_id
                    rented_product_ctx = \
                        self.with_context(
                            location=rental_in_location.id
                        ).product_id.rented_product_id
                    in_location_available_qty =\
                        rented_product_ctx.qty_available -\
                        rented_product_ctx.outgoing_qty
                    compare_qty = float_compare(
                        in_location_available_qty, self.rental_qty,
                        precision_rounding=product_uom.rounding)
                    if compare_qty == -1:
                        res['warning'] = {
                            'title': _("Not enough stock !"),
                            'message': _(
                                "You want to rent %.2f %s but you only "
                                "have %.2f %s currently available on the "
                                "stock location '%s' ! Make sure that you "
                                "get some units back in the mean time or "
                                "re-supply the stock location '%s'.") % (
                                self.rental_qty,
                                product_uom.name,
                                in_location_available_qty,
                                product_uom.name,
                                rental_in_location.name,
                                rental_in_location.name)
                        }
            elif self.product_id.rental_service_ids:
                self.can_sell_rental = True
                self.rental = False
                self.rental_type = False
                self.rental_qty = 0
                self.extension_rental_id = False
            else:
                self.rental_type = False
                self.rental = False
                self.rental_qty = 0
                self.extension_rental_id = False
                self.can_sell_rental = False
                self.sell_rental_id = False
        else:
            self.rental_type = False
            self.rental = False
            self.rental_qty = 0
            self.extension_rental_id = False
            self.can_sell_rental = False
            self.sell_rental_id = False
        return res

    @api.onchange('extension_rental_id')
    def extension_rental_id_change(self):
        if self.product_id and\
                self.rental_type == 'rental_extension' and\
                self.extension_rental_id:
            if self.extension_rental_id.rental_product_id != self.product_id:
                raise UserError(_(
                    "The Rental Service of the Rental Extension you just "
                    "selected is '%s' and it's not the same as the "
                    "Product currently selected in this Sale Order Line.")
                    % self.extension_rental_id.rental_product_id.name)
            initial_end_date = self.extension_rental_id.end_date
            self.start_date = initial_end_date + relativedelta(days=1)
            self.rental_qty = self.extension_rental_id.rental_qty

    @api.onchange('sell_rental_id')
    def sell_rental_id_change(self):
        if self.sell_rental_id:
            self.product_uom_qty = self.sell_rental_id.rental_qty

    @api.onchange('rental_qty', 'number_of_days', 'product_id' , 'rental_guarantee_price')
    def rental_qty_number_of_days_change(self):
        if self.product_id.rented_product_id:
            qty = self.rental_qty * self.number_of_days
            self.product_uom_qty = qty
            self.rental_guarantee_price_total = self.rental_qty * self.rental_guarantee_price

    @api.onchange('rental_type')
    def rental_type_change(self):
        """
        If rental_type field on sale order line was changed the extension_rental_id field will enable to edit.
        on extension_rental_id are filtering by partner_id and rental_product_id.
        """
        if self.rental_type == 'new_rental':
            self.extension_rental_id = False
        else:
            return {'domain': {'extension_rental_id': [('partner_id', '=', self.order_id.partner_id.id),('rental_product_id', '=', self.product_id.id)]}}