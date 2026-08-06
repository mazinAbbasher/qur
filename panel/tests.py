"""Access-control tests: login requirement, role gating, cost hiding.

Run with:  python manage.py test panel
These use an isolated test database; the real db.sqlite3 is never touched.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from panel.models import Product
from panel.permissions import MANAGER_GROUP, SALESPERSON_GROUP


class AccessControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        manager_group, _ = Group.objects.get_or_create(name=MANAGER_GROUP)
        sales_group, _ = Group.objects.get_or_create(name=SALESPERSON_GROUP)

        cls.manager = User.objects.create_user('boss', password='pw12345!x')
        cls.manager.groups.add(manager_group)

        cls.rep = User.objects.create_user('rep', password='pw12345!x')
        cls.rep.groups.add(sales_group)

        cls.product = Product.objects.create(name='Paracetamol', exchange_rate=600)

    # --- Authentication -------------------------------------------------
    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse('panel:sale_list'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/login/', resp['Location'])

    # --- Salesperson allow list ----------------------------------------
    def test_salesperson_allowed_pages(self):
        self.client.force_login(self.rep)
        for name in ['panel:sale_list', 'panel:client_list',
                     'panel:product_list', 'panel:inventory_list']:
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, f"{name} should be allowed")

    def test_salesperson_denied_manager_pages(self):
        self.client.force_login(self.rep)
        home = reverse('panel:sale_list')
        for name in ['panel:shipment_list', 'panel:net_profit_dashboard',
                     'panel:expense_list', 'panel:supplier_list',
                     'panel:employee_list', 'panel:manager_list']:
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 302, f"{name} should be denied")
            self.assertEqual(resp['Location'], home)

    def test_salesperson_index_redirected(self):
        # The dashboard (net profit) is manager-only; reps get bounced home.
        self.client.force_login(self.rep)
        resp = self.client.get(reverse('panel:index'))
        self.assertEqual(resp.status_code, 302)

    # --- Manager unrestricted ------------------------------------------
    def test_manager_allowed_everywhere(self):
        self.client.force_login(self.manager)
        for name in ['panel:index', 'panel:shipment_list',
                     'panel:net_profit_dashboard', 'panel:expense_list']:
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, f"{name} should be allowed for manager")

    # --- Cost hiding in shared page ------------------------------------
    def test_product_detail_hides_cost_for_salesperson(self):
        url = reverse('panel:product_detail', args=[self.product.pk])

        self.client.force_login(self.rep)
        rep_html = self.client.get(url).content.decode()
        self.assertNotIn('تكلفة الوحدة', rep_html)   # purchase-cost table header
        self.assertNotIn('آخر تكلفة', rep_html)      # latest-cost row

        self.client.force_login(self.manager)
        mgr_html = self.client.get(url).content.decode()
        self.assertIn('تكلفة الوحدة', mgr_html)      # manager still sees costs
