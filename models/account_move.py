
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
import odoo.addons.decimal_precision as dp

class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    rental_qty = fields.Float(
        string='Rental Quantity',
        digits=dp.get_precision('Product Unit of Measure'),
        help="Indicate the number of items that will be rented.")