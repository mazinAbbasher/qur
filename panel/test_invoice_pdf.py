"""Sale invoice PDF: discounted lines show the old and new price, and the
totals box adds up to the Grand Total.

Run with:  python manage.py test panel
"""

from datetime import date
from decimal import Decimal
from unittest import mock

import weasyprint
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from panel.models import Inventory, Invoice, Product, ReturnedProduct, Sale, SaleItem, Shipment
from panel.permissions import MANAGER_GROUP


@override_settings(SYNC_ROLE='server')
class InvoicePdfTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.manager = User.objects.create_user('boss', password='pw12345!x')
        cls.manager.groups.add(Group.objects.get_or_create(name=MANAGER_GROUP)[0])
        product = Product.objects.create(name='Amoxicillin')
        shipment = Shipment.objects.create(
            product=product, quantity=100, shipment_cost=0, batch_number='A',
            expiry_date=date(2030, 1, 1), sale_sdg=7500,
        )
        cls.inventory = Inventory.objects.create(product=product, shipment=shipment, quantity=100)

    def _invoice(self, *rows, returns=()):
        """rows: (price, quantity, price_discount %, free_goods_discount %)."""
        sale = Sale.objects.create()
        items = [
            SaleItem.objects.create(
                sale=sale, inventory=self.inventory, price=Decimal(price), quantity=qty,
                price_discount=Decimal(pd), free_goods_discount=Decimal(fg),
            )
            for price, qty, pd, fg in rows
        ]
        sale.calculate_total()
        for index, qty in returns:
            ReturnedProduct.objects.create(sale=sale, sale_item=items[index], quantity=qty)
        sale.refresh_from_db()
        return Invoice.objects.create(sale=sale, total=sale.total)

    def _render(self, invoice):
        """Return the view's template context and the HTML handed to WeasyPrint."""
        self.client.force_login(self.manager)
        with mock.patch('weasyprint.HTML', wraps=weasyprint.HTML) as html:
            resp = self.client.get(reverse('panel:invoice_pdf', args=[invoice.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(b''.join(resp.streaming_content).startswith(b'%PDF'))
        return resp.context, html.call_args.kwargs['string']

    def test_discounted_line_shows_old_and_new_price(self):
        context, html = self._render(self._invoice(('7500', 3, '10', '0')))
        self.assertIn('<s>7,500.00</s>', html)      # old unit price, struck
        self.assertIn('6,818.18', html)             # unit price after discount
        self.assertIn('<s>22,500.00</s>', html)     # old line total, struck
        self.assertIn('20,454.55', html)            # line total after discount
        self.assertEqual(context['discount_total'], Decimal('2045.45'))
        self.assertIn('You saved', html)

    def test_totals_add_up_with_discount_and_return(self):
        # Rounding the discounted prices would leave the rows a cent off the
        # stored total here; the discount row absorbs it.
        invoice = self._invoice(('7500', 3, '10', '0'), returns=[(0, 1)])
        context, _ = self._render(invoice)
        self.assertEqual(context['subtotal'], Decimal('22500'))
        self.assertEqual(context['returns_total'], Decimal('6818.18'))
        self.assertEqual(
            context['subtotal'] - context['discount_total'] - context['returns_total'],
            invoice.sale.total,
        )

    def test_free_units_shown_without_price_discount(self):
        context, html = self._render(self._invoice(('2000', 20, '0', '10')))
        self.assertEqual(context['discount_total'], 0)
        self.assertEqual(context['free_units_total'], 2)
        self.assertIn('+2 free', html)
        self.assertNotIn('<s>', html)
        self.assertNotIn('Subtotal', html)

    def test_plain_invoice_has_no_discount_markup(self):
        _, html = self._render(self._invoice(('950', 2, '0', '0')))
        self.assertNotIn('<s>', html)
        self.assertNotIn('Subtotal', html)
        self.assertNotIn('You saved', html)
        self.assertIn('1,900.00 SDG', html)
