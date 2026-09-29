"""Access-control tests: login requirement, role gating, cost hiding.

Run with:  python manage.py test panel
These use an isolated test database; the real db.sqlite3 is never touched.
"""

import re
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from panel.models import (
    Inventory, Invoice, InvoicePayment, Product, ReturnedProduct, Sale,
    SaleItem, Shipment,
)
from sync.models import SyncOutbox
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

    def test_salesperson_cannot_mark_unpaid(self):
        # mark_unpaid wipes every payment, so it must not bypass the
        # manager-only rule on deleting a single payment.
        self.client.force_login(self.rep)
        resp = self.client.post(reverse('panel:invoice_mark_unpaid', args=[self.invoice.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('panel:sale_list'))
        self.assertTrue(InvoicePayment.objects.filter(pk=self.payment.pk).exists())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'partial')

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


class SaleFormTestMixin:
    """A logged-in manager, one product with batches A (10 units) and B (2),
    and helpers that post the sale create/edit forms like the page does."""

    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user('boss', password='pw12345!x')
        self.manager.groups.add(Group.objects.get_or_create(name=MANAGER_GROUP)[0])
        self.client.force_login(self.manager)
        self.product = Product.objects.create(name='Med A', exchange_rate=600)
        self.inv_a = self._batch('A', 10)
        self.inv_b = self._batch('B', 2)

    def _batch(self, number, qty, sdg='2000'):
        shipment = Shipment.objects.create(
            product=self.product, quantity=qty, shipment_cost=Decimal('10'),
            cost_sdg=Decimal('1200'), sale_sdg=Decimal(sdg),
            batch_number=number, expiry_date=date(2030, 1, 1), exchange_rate=600,
        )
        return Inventory.objects.create(product=self.product, shipment=shipment, quantity=qty)

    def _data(self, rows, initial=0):
        data = {
            'due_date': date.today().isoformat(),
            'items-TOTAL_FORMS': str(len(rows)), 'items-INITIAL_FORMS': str(initial),
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
        }
        for i, row in enumerate(rows):
            data.update({f'items-{i}-{k}': v for k, v in row.items()})
        return data

    def _row(self, inventory, qty, free=0, **extra):
        return {'product': self.product.pk, 'batch': inventory.pk, 'quantity': qty,
                'free_goods_discount': free, 'price_discount': 0, **extra}

    def _create(self, rows):
        return self.client.post(reverse('panel:sale_create'), self._data(rows))

    def _edit(self, sale, rows):
        initial = sum(1 for r in rows if 'id' in r)
        return self.client.post(reverse('panel:sale_edit', args=[sale.pk]),
                                self._data(rows, initial=initial))

    def _sale_with(self, qty):
        self._create([self._row(self.inv_a, qty)])
        sale = Sale.objects.get()
        return sale, sale.items.get()

    def stock(self):
        self.inv_a.refresh_from_db()
        self.inv_b.refresh_from_db()
        return self.inv_a.quantity, self.inv_b.quantity

    def errors(self, resp):
        return [str(m) for m in get_messages(resp.wsgi_request) if m.level_tag == 'error']


@override_settings(SYNC_ROLE='server')
class SaleStockTests(SaleFormTestMixin, TransactionTestCase):
    """Stock moves exactly with what a sale holds; oversells are refused whole.

    TransactionTestCase (real commits) because TestCase's wrapping transaction
    would hide exactly the commit/rollback behaviour under test.
    """
    serialized_rollback = True  # keep migration-seeded rows for later tests

    def assertNothingSaved(self):
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(SaleItem.objects.count(), 0)
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(self.stock(), (10, 2))

    # --- create ------------------------------------------------------------
    def test_valid_sale_saves_everything(self):
        resp = self._create([self._row(self.inv_a, 3)])
        sale = Sale.objects.get()
        self.assertRedirects(resp, reverse('panel:sale_detail', args=[sale.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(sale.items.count(), 1)
        self.assertEqual(Invoice.objects.get().sale, sale)
        self.assertEqual(self.stock(), (7, 2))

    def test_create_same_batch_in_two_rows_deducts_both(self):
        self._create([self._row(self.inv_a, 3), self._row(self.inv_a, 4)])
        self.assertEqual(self.stock(), (3, 2))

    def test_create_deducts_free_goods(self):
        self._create([self._row(self.inv_a, 5, free=20)])     # 5 paid + 1 free
        self.assertEqual(self.stock(), (4, 2))

    def test_create_can_sell_exactly_all_stock(self):
        self._create([self._row(self.inv_a, 6), self._row(self.inv_a, 4)])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock(), (0, 2))

    def test_create_oversell_in_one_row_is_refused(self):
        resp = self._create([self._row(self.inv_a, 3), self._row(self.inv_b, 5)])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.errors(resp), [
            'الكمية المطلوبة (مع المجاني) 5 غير متوفرة في الدفعة B للمنتج Med A. المتاح: 2'])
        self.assertContains(resp, 'المتاح: 2')        # shown on the re-rendered form
        self.assertNothingSaved()

    def test_create_oversell_split_across_rows_is_refused(self):
        resp = self._create([self._row(self.inv_a, 6), self._row(self.inv_a, 6)])
        self.assertEqual(self.errors(resp), [
            'الكمية المطلوبة (مع المجاني) 12 غير متوفرة في الدفعة A للمنتج Med A. المتاح: 10'])
        self.assertNothingSaved()

    def test_create_oversell_via_free_goods_is_refused(self):
        resp = self._create([self._row(self.inv_b, 2, free=50)])  # 2 paid + 1 free
        self.assertEqual(len(self.errors(resp)), 1)
        self.assertNothingSaved()

    def test_create_reports_every_short_batch(self):
        resp = self._create([self._row(self.inv_a, 11), self._row(self.inv_b, 3)])
        self.assertEqual(len(self.errors(resp)), 2)
        self.assertNothingSaved()

    @override_settings(SYNC_ROLE='manager')
    def test_missing_batch_rolls_back_and_is_not_queued_for_sync(self):
        # 'inventory' passes form validation but no 'batch' was chosen.
        row = self._row(self.inv_a, 1)
        del row['batch']
        row['inventory'] = self.inv_a.pk
        resp = self._create([row])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.errors(resp), ['يجب اختيار دفعة لكل منتج.'])
        self.assertNothingSaved()
        self.assertFalse(SyncOutbox.objects.filter(model_label='panel.Sale').exists())

    # --- edit --------------------------------------------------------------
    def test_edit_without_changes_keeps_stock(self):
        sale, item = self._sale_with(3)
        resp = self._edit(sale, [self._row(self.inv_a, 3, id=item.pk)])
        self.assertRedirects(resp, reverse('panel:sale_detail', args=[sale.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(self.stock(), (7, 2))

    def test_edit_quantity_up_and_down(self):
        sale, item = self._sale_with(3)
        self._edit(sale, [self._row(self.inv_a, 5, id=item.pk)])
        self.assertEqual(self.stock(), (5, 2))
        self._edit(sale, [self._row(self.inv_a, 1, id=item.pk)])
        self.assertEqual(self.stock(), (9, 2))

    def test_edit_add_row_on_same_batch(self):
        sale, item = self._sale_with(3)
        self._edit(sale, [self._row(self.inv_a, 3, id=item.pk), self._row(self.inv_a, 4)])
        self.assertEqual(self.stock(), (3, 2))

    def test_edit_move_item_to_other_batch(self):
        sale, item = self._sale_with(2)
        self._edit(sale, [self._row(self.inv_b, 2, id=item.pk)])
        self.assertEqual(self.stock(), (10, 0))

    def test_edit_delete_row_restores_stock(self):
        self._create([self._row(self.inv_a, 3), self._row(self.inv_b, 1)])
        sale = Sale.objects.get()
        a_item, b_item = sale.items.order_by('pk')
        self._edit(sale, [self._row(self.inv_a, 3, id=a_item.pk),
                          self._row(self.inv_b, 1, id=b_item.pk, DELETE='on')])
        self.assertEqual(self.stock(), (7, 2))

    def test_edit_can_use_own_stock_plus_remaining(self):
        sale, item = self._sale_with(3)                     # stock 7, sale holds 3
        self._edit(sale, [self._row(self.inv_a, 10, id=item.pk)])
        self.assertEqual(self.stock(), (0, 2))

    def test_edit_oversell_is_refused_and_nothing_changes(self):
        sale, item = self._sale_with(3)
        resp = self._edit(sale, [self._row(self.inv_a, 11, id=item.pk)])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.errors(resp), [
            'الكمية المطلوبة (مع المجاني) 11 غير متوفرة في الدفعة A للمنتج Med A. المتاح: 10'])
        self.assertEqual(self.stock(), (7, 2))
        item.refresh_from_db()
        self.assertEqual(item.quantity, 3)

    def test_edit_oversell_split_across_rows_is_refused(self):
        sale, item = self._sale_with(3)
        resp = self._edit(sale, [self._row(self.inv_a, 6, id=item.pk), self._row(self.inv_a, 5)])
        self.assertEqual(len(self.errors(resp)), 1)
        self.assertEqual(self.stock(), (7, 2))
        self.assertEqual(SaleItem.objects.count(), 1)

    def _page_limits(self, resp):
        # The form's max attributes and submit check read inventoryData.
        html = resp.content.decode()
        return {pk: int(q) for pk, q in re.findall(r'"(\d+)": \{[^}]*"quantity": "(\d+)"', html)}

    def test_form_limits_use_stock_available_to_this_sale(self):
        sale, item = self._sale_with(3)                     # A: stock 7, sale holds 3
        create = self._page_limits(self.client.get(reverse('panel:sale_create')))
        self.assertEqual(create, {str(self.inv_a.pk): 7, str(self.inv_b.pk): 2})
        edit = self._page_limits(self.client.get(reverse('panel:sale_edit', args=[sale.pk])))
        self.assertEqual(edit, {str(self.inv_a.pk): 10, str(self.inv_b.pk): 2})

    def test_edit_counts_returned_units(self):
        sale, item = self._sale_with(5)                     # stock 5
        ReturnedProduct.objects.create(sale=sale, sale_item=item, quantity=2)
        self.assertEqual(self.stock(), (7, 2))              # sale now holds 3
        self._edit(sale, [self._row(self.inv_a, 5, id=item.pk)])
        self.assertEqual(self.stock(), (7, 2))
        self._edit(sale, [self._row(self.inv_a, 4, id=item.pk)])
        self.assertEqual(self.stock(), (8, 2))


@override_settings(SYNC_ROLE='server')
class SaleEditPriceTests(SaleFormTestMixin, TestCase):
    """Editing a sale never re-prices what was already sold.

    A saved row that stays on its batch keeps its sold price; only a new row,
    or one moved to another batch, takes that batch's current SDG price.
    """

    def setUp(self):
        super().setUp()
        self.inv_c = self._batch('C', 5, sdg='2500')

    def _set_price(self, inventory, sdg=None, usd=None):
        shipment = inventory.shipment
        shipment.sale_sdg = Decimal(sdg) if sdg else shipment.sale_sdg
        shipment.sale_usd = Decimal(usd) if usd else shipment.sale_usd
        shipment.save()

    def _prices(self, sale):
        return [item.price for item in sale.items.order_by('pk')]

    def _totals(self, sale):
        sale.refresh_from_db()
        return sale.total, Invoice.objects.get(sale=sale).total

    def test_editing_only_the_due_date_keeps_prices(self):
        # Used to zero the invoice: the shipment has an SDG price but no USD one.
        sale, item = self._sale_with(3)
        new_due = date.today() + timedelta(days=7)
        data = self._data([self._row(self.inv_a, 3, id=item.pk)], initial=1)
        data['due_date'] = new_due.isoformat()
        self.client.post(reverse('panel:sale_edit', args=[sale.pk]), data)
        self.assertEqual(self._prices(sale), [Decimal('2000')])
        self.assertEqual(self._totals(sale), (Decimal('6000'), Decimal('6000')))
        self.assertEqual(Invoice.objects.get(sale=sale).due_date, new_due)

    def test_edit_ignores_legacy_usd_price(self):
        self._set_price(self.inv_a, usd='3')            # 3 USD x 600 = 1800 != 2000
        sale, item = self._sale_with(3)
        self._edit(sale, [self._row(self.inv_a, 3, id=item.pk)])
        self.assertEqual(self._prices(sale), [Decimal('2000')])

    def test_edit_keeps_sold_price_after_price_change(self):
        sale, item = self._sale_with(3)
        self._set_price(self.inv_a, sdg='3000')
        self._edit(sale, [self._row(self.inv_a, 5, id=item.pk)])
        self.assertEqual(self._prices(sale), [Decimal('2000')])
        self.assertEqual(self._totals(sale), (Decimal('10000'), Decimal('10000')))

    def test_edit_discount_keeps_sold_base_price(self):
        sale, item = self._sale_with(3)
        self._set_price(self.inv_a, sdg='3000')
        self._edit(sale, [self._row(self.inv_a, 3, id=item.pk, price_discount=10)])
        item.refresh_from_db()
        self.assertEqual((item.price, item.price_discount), (Decimal('2000'), Decimal('10')))

    def test_row_moved_to_other_batch_takes_its_current_price(self):
        sale, item = self._sale_with(3)
        self._edit(sale, [self._row(self.inv_c, 3, id=item.pk)])
        self.assertEqual(self._prices(sale), [Decimal('2500')])

    def test_new_row_takes_current_price(self):
        sale, item = self._sale_with(3)
        self._set_price(self.inv_a, sdg='3000')
        self._edit(sale, [self._row(self.inv_a, 3, id=item.pk), self._row(self.inv_a, 1)])
        self.assertEqual(self._prices(sale), [Decimal('2000'), Decimal('3000')])

    def test_edit_page_shows_sold_price(self):
        sale, item = self._sale_with(3)
        self._set_price(self.inv_a, sdg='3000')
        html = self.client.get(reverse('panel:sale_edit', args=[sale.pk])).content.decode()
        self.assertRegex(html, r'name="items-0-price" value="2000\.00"')
