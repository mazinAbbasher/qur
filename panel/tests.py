"""Access-control tests: login requirement, role gating, cost hiding.

Run with:  python manage.py test panel
These use an isolated test database; the real db.sqlite3 is never touched.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from panel.models import Invoice, InvoicePayment, Product, Sale
from panel.permissions import MANAGER_GROUP, SALESPERSON_GROUP


@override_settings(SYNC_ROLE='server')  # deterministic regardless of local .env
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

    # --- Salesperson: broad operational access -------------------------
    def test_salesperson_allowed_pages(self):
        self.client.force_login(self.rep)
        for name in ['panel:sale_list', 'panel:client_list',
                     'panel:product_list', 'panel:inventory_list',
                     'panel:shipment_list', 'panel:employee_list',
                     'panel:manager_list', 'panel:supplier_list',
                     'panel:area_list', 'panel:lost_product_list',
                     # Reps log their own daily field expenses.
                     'panel:expense_list', 'panel:expense_add']:
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, f"{name} should be allowed")

    def test_salesperson_denied_financial_pages(self):
        self.client.force_login(self.rep)
        home = reverse('panel:sale_list')
        denied = [
            'panel:index', 'panel:net_profit_dashboard',
            'panel:shipment_profit_report',
            'panel:sale_commissions',
            # view-only entities: create/edit blocked
            'panel:product_add',
            'panel:shipment_create', 'panel:employee_add',
            'panel:manager_add', 'panel:supplier_add',
            # whole finance app blocked by path prefix
            'financial_dashboard',
        ]
        for name in denied:
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

    def test_shipment_list_hides_costs_for_salesperson(self):
        url = reverse('panel:shipment_list')

        self.client.force_login(self.rep)
        self.assertNotIn('تكلفة', self.client.get(url).content.decode())

        self.client.force_login(self.manager)
        self.assertIn('تكلفة', self.client.get(url).content.decode())


@override_settings(SYNC_ROLE='server')
class InvoicePaymentDeleteTests(TestCase):
    """A payment recorded by mistake can be removed, unblocking sale delete."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.manager = User.objects.create_user('boss', password='pw12345!x')
        cls.manager.groups.add(Group.objects.get_or_create(name=MANAGER_GROUP)[0])
        cls.rep = User.objects.create_user('rep', password='pw12345!x')
        cls.rep.groups.add(Group.objects.get_or_create(name=SALESPERSON_GROUP)[0])

    def setUp(self):
        self.sale = Sale.objects.create(total=100)
        self.invoice = Invoice.objects.create(sale=self.sale, total=100)
        self.payment = InvoicePayment.objects.create(invoice=self.invoice, amount=40)
        self.url = reverse('panel:invoice_delete_payment',
                           args=[self.invoice.pk, self.payment.pk])

    def test_manager_deletes_payment_and_status_resets(self):
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'partial')

        self.client.force_login(self.manager)
        resp = self.client.post(self.url)
        self.assertRedirects(resp, reverse('panel:sale_detail', args=[self.sale.pk]),
                             fetch_redirect_response=False)
        self.assertFalse(InvoicePayment.objects.filter(pk=self.payment.pk).exists())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'unpaid')

        # With the payment gone the sale (and its invoice) can be deleted.
        self.client.post(reverse('panel:sale_delete', args=[self.sale.pk]))
        self.assertFalse(Sale.objects.filter(pk=self.sale.pk).exists())
        self.assertFalse(Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_salesperson_cannot_delete_payment(self):
        self.client.force_login(self.rep)
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('panel:sale_list'))
        self.assertTrue(InvoicePayment.objects.filter(pk=self.payment.pk).exists())

    def test_payment_must_belong_to_invoice(self):
        other = Invoice.objects.create(sale=Sale.objects.create(total=50), total=50)
        self.client.force_login(self.manager)
        resp = self.client.post(reverse('panel:invoice_delete_payment',
                                        args=[other.pk, self.payment.pk]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(InvoicePayment.objects.filter(pk=self.payment.pk).exists())

    def test_delete_button_shown_to_manager_only(self):
        for name in ('panel:sale_detail', 'panel:invoice_detail'):
            pk = self.sale.pk if name == 'panel:sale_detail' else self.invoice.pk
            page = reverse(name, args=[pk])
            self.client.force_login(self.manager)
            self.assertContains(self.client.get(page), self.url)
            self.client.force_login(self.rep)
            self.assertNotContains(self.client.get(page), self.url)

    def test_get_not_allowed(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_mark_unpaid_redirects_to_its_own_invoice(self):
        # Invoice and sale ids drift apart in real data (e.g. a sale saved
        # without an invoice), so the redirect must use the invoice's own pk.
        Sale.objects.create(total=10)                    # sale with no invoice
        sale = Sale.objects.create(total=100)
        invoice = Invoice.objects.create(sale=sale, total=100)
        other = Invoice.objects.create(sale=Sale.objects.create(total=5), total=5)
        InvoicePayment.objects.create(invoice=invoice, amount=40)
        self.assertNotEqual(invoice.pk, sale.pk)

        self.client.force_login(self.manager)
        resp = self.client.post(reverse('panel:invoice_mark_unpaid', args=[invoice.pk]))
        self.assertRedirects(resp, reverse('panel:invoice_detail', args=[invoice.pk]))
        self.assertFalse(invoice.payments.exists())
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'unpaid')
        # The other customer's invoice is untouched by the redirect target.
        self.assertTrue(InvoicePayment.objects.filter(invoice=self.invoice).exists())
        self.assertEqual(Invoice.objects.get(pk=other.pk).status, 'unpaid')
