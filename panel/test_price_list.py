"""Customer price-list PDF on the product list page.

Run with:  python manage.py test panel
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from panel.models import Inventory, Product, Shipment
from panel.permissions import MANAGER_GROUP, SALESPERSON_GROUP
from panel.views import _attach_sale_prices


def make_batch(product, sale_sdg, in_stock=10, expires_in_days=365, received_days_ago=0):
    shipment = Shipment.objects.create(
        product=product, quantity=in_stock or 1, shipment_cost=0,
        batch_number=f'B{Shipment.objects.count() + 1}',
        expiry_date=date.today() + timedelta(days=expires_in_days),
        received_at=timezone.now() - timedelta(days=received_days_ago),
        sale_sdg=sale_sdg,
    )
    Inventory.objects.create(product=product, shipment=shipment, quantity=in_stock)
    return shipment


class SalePriceTests(TestCase):
    """Which batch's price the price list quotes."""

    def price_of(self, product):
        return _attach_sale_prices([product])[0]

    def test_newest_sellable_batch_wins(self):
        product = Product.objects.create(name='Amoxicillin')
        make_batch(product, 1000, received_days_ago=30)
        make_batch(product, 1200, received_days_ago=1)
        p = self.price_of(product)
        self.assertEqual(p.sale_price, Decimal('1200'))
        self.assertTrue(p.in_stock)

    def test_sold_out_and_expired_batches_are_skipped(self):
        product = Product.objects.create(name='Ibuprofen')
        good = make_batch(product, 900, received_days_ago=30)
        make_batch(product, 1100, in_stock=0, received_days_ago=5)            # sold out
        make_batch(product, 1300, expires_in_days=-1, received_days_ago=1)   # expired
        p = self.price_of(product)
        self.assertEqual(p.sale_price, Decimal('900'))
        self.assertEqual(p.sale_expiry, good.expiry_date)
        self.assertTrue(p.in_stock)

    def test_nothing_sellable_falls_back_to_latest_shipment(self):
        product = Product.objects.create(name='Cetirizine')
        make_batch(product, 500, in_stock=0, received_days_ago=10)
        make_batch(product, 650, in_stock=0, received_days_ago=2)
        p = self.price_of(product)
        self.assertEqual(p.sale_price, Decimal('650'))
        self.assertFalse(p.in_stock)
        self.assertIsNone(p.sale_expiry)

    def test_never_shipped_has_no_price(self):
        p = self.price_of(Product.objects.create(name='New item'))
        self.assertIsNone(p.sale_price)
        self.assertFalse(p.in_stock)


@override_settings(SYNC_ROLE='server')
class PriceListPdfTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.manager = User.objects.create_user('boss', password='pw12345!x')
        cls.manager.groups.add(Group.objects.get_or_create(name=MANAGER_GROUP)[0])
        cls.rep = User.objects.create_user('rep', password='pw12345!x')
        cls.rep.groups.add(Group.objects.get_or_create(name=SALESPERSON_GROUP)[0])

        cls.product = Product.objects.create(name='Paracetamol', unit='Box', description='500mg')
        make_batch(cls.product, 2500)
        cls.url = reverse('panel:product_price_list_pdf')

    def test_manager_and_salesperson_get_pdf(self):
        for user in (self.manager, self.rep):
            self.client.force_login(user)
            resp = self.client.get(self.url, {'ids': [self.product.pk], 'client': 'Al Shifa', 'expiry': '1'})
            self.assertEqual(resp.status_code, 200, user.username)
            self.assertEqual(resp['Content-Type'], 'application/pdf')
            self.assertTrue(b''.join(resp.streaming_content).startswith(b'%PDF'))

    def test_no_selection_redirects_back(self):
        self.client.force_login(self.rep)
        for params in ({}, {'ids': ['abc']}, {'ids': ['999999']}):
            resp = self.client.get(self.url, params)
            self.assertRedirects(resp, reverse('panel:product_list'), fetch_redirect_response=False)

    def test_product_list_offers_selection_and_price(self):
        self.client.force_login(self.rep)
        html = self.client.get(reverse('panel:product_list')).content.decode()
        self.assertIn(f'name="ids" value="{self.product.pk}"', html)
        self.assertIn('2,500', html)
