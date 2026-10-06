"""Access-control tests: login requirement, role gating, cost hiding.

Run with:  python manage.py test panel
These use an isolated test database; the real db.sqlite3 is never touched.
"""

import re
from datetime import date, datetime, timedelta, timezone as dt_timezone
from importlib import import_module
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils.timezone import localtime

from finance.models import Currency, CurrencyExchange, Partner, PartnerTransaction
from panel.models import (
    Commission, CommissionPayment, Employee, Inventory, Invoice, InvoicePayment, Manager,
    ManagerCommissionPayment, Product, ReturnedProduct, Sale, SaleItem, Shipment,
)
from sync.models import SyncOutbox, SyncTombstone
from sync.tracking import apply_guard
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
                     'panel:expense_list', 'panel:expense_add',
                     # ...and the currency exchanges they make.
                     'currency_purchases_list', 'currency_purchase_add']:
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
            # rest of the finance app blocked by path prefix
            'financial_dashboard', 'partners_list', 'partner_add',
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

    # --- Currency exchange: reps record it, without finance data --------
    def test_sidebar_shows_exchange_but_not_finance_for_salesperson(self):
        self.client.force_login(self.rep)
        html = self.client.get(reverse('panel:sale_list')).content.decode()
        self.assertIn(reverse('currency_purchases_list'), html)
        self.assertNotIn(reverse('financial_dashboard'), html)
        self.assertNotIn(reverse('partners_list'), html)

    def _exchange_post(self):
        sdg = Currency.objects.get(code='SDG')
        usd = Currency.objects.get(code='USD')
        return {'bought_currency': usd.pk, 'bought_amount': '100',
                'sold_currency': sdg.pk, 'sold_amount': '600000',
                'date': '2026-09-01', 'note': ''}

    def _deposit_sdg(self, amount):
        PartnerTransaction.objects.create(
            partner=Partner.objects.create(full_name='P'), transaction_type='deposit',
            amount=Decimal(amount), currency=Currency.objects.get(code='SDG'))

    def test_salesperson_blocked_without_balance_and_never_sees_it(self):
        self._deposit_sdg('12345')          # short of the 600,000 being sold
        self.client.force_login(self.rep)
        resp = self.client.post(reverse('currency_purchase_add'), self._exchange_post())
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('غير كافٍ', html)
        self.assertNotIn('12345', html)
        self.assertNotIn('12,345', html)
        self.assertFalse(CurrencyExchange.objects.exists())

    def test_salesperson_adds_exchange_with_enough_balance(self):
        self._deposit_sdg('1000000')
        self.client.force_login(self.rep)
        resp = self.client.post(reverse('currency_purchase_add'), self._exchange_post())
        self.assertRedirects(resp, reverse('currency_purchases_list'),
                             fetch_redirect_response=False)
        ex = CurrencyExchange.objects.get()
        self.assertEqual(ex.exchange_rate, Decimal('6000'))

    def test_salesperson_cannot_edit_or_delete_exchange(self):
        ex = CurrencyExchange.objects.create(
            sold_currency=Currency.objects.get(code='SDG'),
            bought_currency=Currency.objects.get(code='USD'),
            sold_amount=Decimal('600000'), bought_amount=Decimal('100'),
            exchange_rate=Decimal('6000'))
        self.client.force_login(self.rep)
        html = self.client.get(reverse('currency_purchases_list')).content.decode()
        self.assertNotIn(reverse('currency_purchase_edit', args=[ex.pk]), html)
        for name in ['currency_purchase_edit', 'currency_purchase_delete']:
            resp = self.client.post(reverse(name, args=[ex.pk]))
            self.assertEqual(resp['Location'], reverse('panel:sale_list'))
        self.assertTrue(CurrencyExchange.objects.filter(pk=ex.pk).exists())

    def test_manager_exchange_still_checks_balance(self):
        self.client.force_login(self.manager)
        resp = self.client.post(reverse('currency_purchase_add'), self._exchange_post())
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Insufficient balance', resp.content.decode())
        self.assertFalse(CurrencyExchange.objects.exists())


@override_settings(SYNC_ROLE='salesperson')
class SalespersonLaptopExchangeTests(TestCase):
    """On a rep laptop the server checks the balance and records the exchange
    (sync.client.submit_currency_exchange); the network is mocked here."""

    @classmethod
    def setUpTestData(cls):
        cls.rep = get_user_model().objects.create_user('rep', password='pw12345!x')
        cls.rep.groups.add(Group.objects.get_or_create(name=SALESPERSON_GROUP)[0])

    def setUp(self):
        self.client.force_login(self.rep)
        self.sdg = Currency.objects.get(code='SDG')
        self.post = {'bought_currency': Currency.objects.get(code='USD').pk,
                     'bought_amount': '100', 'sold_currency': self.sdg.pk,
                     'sold_amount': '600000', 'date': '2026-09-01', 'note': ''}

    def _submit(self, server_reply):
        with mock.patch('sync.client.push') as push, \
                mock.patch('sync.client._post', side_effect=server_reply) as post:
            resp = self.client.post(reverse('currency_purchase_add'), self.post)
        return resp, push, post

    def test_server_accepts_exchange_is_saved_locally_not_queued(self):
        resp, push, post = self._submit(lambda path, payload: {'ok': True})
        self.assertEqual(resp.status_code, 302)
        push.assert_called_once()          # rep's pending payments go up first
        row = post.call_args.args[1]['row']
        self.assertEqual(row['fields']['sold_currency'], str(self.sdg.sync_id))
        ex = CurrencyExchange.objects.get()
        self.assertEqual(str(ex.sync_id), row['sync_id'])
        # The server already has it; pushing would be rejected anyway.
        self.assertFalse(SyncOutbox.objects.filter(
            model_label='finance.CurrencyExchange').exists())

    def test_server_refuses_short_balance(self):
        short = requests.Response()
        short.status_code = 409
        resp, _, _ = self._submit(requests.HTTPError(response=short))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('غير كافٍ', resp.content.decode())
        self.assertFalse(CurrencyExchange.objects.exists())

    def test_offline_is_refused(self):
        resp, _, _ = self._submit(requests.ConnectionError())
        self.assertEqual(resp.status_code, 200)
        self.assertIn('تعذّر الاتصال بالخادم', resp.content.decode())
        self.assertFalse(CurrencyExchange.objects.exists())


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


@override_settings(SYNC_ROLE='server')
class CommissionTests(SaleFormTestMixin, TestCase):
    """Every sale with an employee carries its commission, so the employee pages
    show it; it follows the sale's total and the employee's percentage."""

    def setUp(self):
        super().setUp()
        self.employee = Employee.objects.create(name='Rep', commission_percentage=Decimal('10'))

    def _sell(self, qty, employee=None):
        data = self._data([self._row(self.inv_a, qty)])
        data['employee'] = (employee or self.employee).pk
        self.client.post(reverse('panel:sale_create'), data)
        return Sale.objects.latest('pk')

    def _amount(self, sale, employee=None):
        return Commission.objects.get(employee=employee or self.employee, sale=sale).amount

    def test_sale_gets_commission(self):
        sale = self._sell(3)  # 3 x 2000
        self.assertEqual(self._amount(sale), Decimal('600'))

    def test_sale_at_zero_percent_still_gets_one(self):
        unpaid = Employee.objects.create(name='New rep')
        sale = self._sell(3, employee=unpaid)
        self.assertEqual(self._amount(sale, unpaid), Decimal('0'))

    def test_return_and_its_removal_follow_the_total(self):
        sale = self._sell(3)
        returned = ReturnedProduct.objects.create(
            sale=sale, sale_item=sale.items.get(), quantity=1)
        self.assertEqual(self._amount(sale), Decimal('400'))
        returned.delete()
        self.assertEqual(self._amount(sale), Decimal('600'))

    def test_setting_percentage_fills_in_missing_commissions(self):
        # A sale saved without a commission (e.g. one synced up from a
        # salesperson laptop before the server created them).
        sale = Sale.objects.create(employee=self.employee, total=Decimal('5000000'))
        self.assertFalse(Commission.objects.exists())
        self.employee.commission_percentage = Decimal('12')
        self.employee.save()
        commission = Commission.objects.get(employee=self.employee, sale=sale)
        self.assertEqual(commission.amount, Decimal('600000'))
        self.assertEqual(commission.created_at, sale.created_at)
        self.assertEqual(self.employee.get_monthly_commission(
            sale.created_at.month, sale.created_at.year), Decimal('600000'))

    def test_zero_commission_follows_new_percentage(self):
        zero = Employee.objects.create(name='New rep')
        sale = self._sell(3, employee=zero)
        zero.commission_percentage = Decimal('5')
        zero.save()
        self.assertEqual(self._amount(sale, zero), Decimal('300'))

    def _reassign(self, sale, employee):
        item = sale.items.get()
        data = self._data([self._row(self.inv_a, item.quantity, id=item.pk)], initial=1)
        data['employee'] = employee.pk if employee else ''
        return self.client.post(reverse('panel:sale_edit', args=[sale.pk]), data)

    def test_moving_sale_moves_its_commission(self):
        sale = self._sell(3)
        other = Employee.objects.create(name='Other', commission_percentage=Decimal('5'))
        self._reassign(sale, other)
        self.assertEqual(self._amount(sale), Decimal('0'))
        self.assertEqual(self._amount(sale, other), Decimal('300'))
        month, year = sale.created_at.month, sale.created_at.year
        self.assertEqual(self.employee.get_monthly_commission(month, year), Decimal('0'))
        self.assertEqual(other.get_monthly_commission(month, year), Decimal('300'))

    def test_removing_employee_drops_commission(self):
        sale = self._sell(3)
        self._reassign(sale, None)
        self.assertEqual(self._amount(sale), Decimal('0'))

    def test_paid_part_stays_with_old_employee(self):
        # e.g. a salesperson laptop reassigns a sale whose commission was partly paid
        sale = self._sell(3)
        Commission.objects.filter(sale=sale).update(paid_amount=Decimal('100'))
        other = Employee.objects.create(name='Other', commission_percentage=Decimal('5'))
        sale.employee = other
        sale.calculate_total()
        old = Commission.objects.get(employee=self.employee, sale=sale)
        self.assertEqual((old.amount, old.unpaid_amount), (Decimal('100'), Decimal('0')))
        self.assertEqual(self._amount(sale, other), Decimal('300'))

    def test_paid_part_of_any_employee_is_owed_back_on_delete(self):
        sale = self._sell(3)
        local = localtime(sale.created_at)
        payment = CommissionPayment.objects.create(
            employee=self.employee, amount=Decimal('100'),
            period_month=local.month, period_year=local.year)
        # Moved to another employee since (e.g. on a salesperson laptop): the
        # paid part stays the first one's.
        other = Employee.objects.create(name='Other', commission_percentage=Decimal('5'))
        sale.employee = other
        sale.calculate_total()
        self.client.post(reverse('panel:sale_delete', args=[sale.pk]))
        self.assertFalse(Sale.objects.filter(pk=sale.pk).exists())
        payment.refresh_from_db()
        self.assertEqual(payment.owed_back, Decimal('100'))
        self.assertEqual(self.employee.get_unpaid_commission(local.month, local.year), Decimal('-100'))
        self.assertEqual(other.get_unpaid_commission(local.month, local.year), Decimal('0'))

    @override_settings(SYNC_ROLE='salesperson')
    def test_salesperson_laptop_leaves_commissions_to_server(self):
        sale = Sale.objects.create(employee=self.employee, total=Decimal('1000'))
        sale.calculate_total()
        self.employee.save()
        self.assertFalse(Commission.objects.exists())


def _run_migration(name, function):
    """Call a data migration's function with the models as of that migration,
    as ``migrate`` does (no custom save() methods or signals)."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    apps = MigrationExecutor(connection).loader.project_state(('panel', name)).apps
    getattr(import_module(f'panel.migrations.{name}'), function)(apps, None)


@override_settings(SYNC_ROLE='server')
class BackfillCommissionsMigrationTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(name='Rep')
        # Raise the percentage without the signal, as an old install would hold it.
        Employee.objects.filter(pk=self.employee.pk).update(commission_percentage=Decimal('10'))

    def _backfill(self):
        _run_migration('0010_backfill_commissions', 'backfill_commissions')

    def _shrink_reassigned(self):
        _run_migration('0011_reassigned_sale_commissions', 'shrink_reassigned_commissions')

    def _sale(self, total, when=None):
        return Sale.objects.create(employee=self.employee, total=Decimal(total),
                                   created_at=when or datetime(2026, 5, 3, tzinfo=dt_timezone.utc))

    def test_missing_commission_created_in_the_sale_month(self):
        sale = self._sale('5000000')
        self._backfill()
        commission = Commission.objects.get(sale=sale)
        self.assertEqual(commission.amount, Decimal('500000'))
        self.assertEqual(commission.created_at, sale.created_at)
        self.assertEqual(self.employee.get_monthly_commission(5, 2026), Decimal('500000'))
        self.assertEqual(self.employee.get_unpaid_commission(5, 2026), Decimal('500000'))

    def test_unpaid_commission_recomputed_paid_one_kept(self):
        stale = self._sale('1000')
        Commission.objects.create(employee=self.employee, sale=stale, amount=Decimal('0'))
        paid = self._sale('1000')
        Commission.objects.create(employee=self.employee, sale=paid,
                                  amount=Decimal('50'), paid_amount=Decimal('20'))
        self._backfill()
        self.assertEqual(Commission.objects.get(sale=stale).amount, Decimal('100'))
        self.assertEqual(Commission.objects.get(sale=paid).amount, Decimal('50'))

    def test_moved_sale_no_longer_counts_for_old_employee(self):
        other = Employee.objects.create(name='Other')
        Employee.objects.filter(pk=other.pk).update(commission_percentage=Decimal('5'))
        moved = self._sale('1000')
        Commission.objects.create(employee=self.employee, sale=moved, amount=Decimal('100'))
        paid = self._sale('1000')
        Commission.objects.create(employee=self.employee, sale=paid,
                                  amount=Decimal('100'), paid_amount=Decimal('30'))
        Sale.objects.filter(pk__in=[moved.pk, paid.pk]).update(employee=other)
        self._backfill()
        self._shrink_reassigned()
        self.assertEqual(Commission.objects.get(employee=self.employee, sale=moved).amount, Decimal('0'))
        self.assertEqual(Commission.objects.get(employee=self.employee, sale=paid).amount, Decimal('30'))
        self.assertEqual(Commission.objects.get(employee=other, sale=moved).amount, Decimal('50'))
        self.assertEqual(self.employee.get_monthly_commission(5, 2026), Decimal('30'))

    def test_sale_without_employee_skipped(self):
        Sale.objects.create(total=Decimal('1000'))
        self._backfill()
        self.assertFalse(Commission.objects.exists())

    @override_settings(SYNC_ROLE='salesperson')
    def test_salesperson_laptop_skipped(self):
        self._sale('1000')
        self._backfill()
        self.assertFalse(Commission.objects.exists())


def _local(y, m, d):
    return datetime(y, m, d, 12, tzinfo=dt_timezone.utc)


@override_settings(SYNC_ROLE='server')
class CommissionPaymentTests(TestCase):
    """A payment recorded for a month clears that month's commission, even when
    older months still have unpaid commissions."""

    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user('boss', password='pw12345!x')
        self.manager.groups.add(Group.objects.get_or_create(name=MANAGER_GROUP)[0])
        self.client.force_login(self.manager)
        self.employee = Employee.objects.create(name='Rep', commission_percentage=Decimal('10'))
        self.june = Sale.objects.create(employee=self.employee, total=Decimal('3000000'),
                                        created_at=_local(2026, 6, 10))
        self.september = Sale.objects.create(employee=self.employee, total=Decimal('5000000'),
                                             created_at=_local(2026, 9, 10))
        self.employee.recalculate_commissions()

    def unpaid(self, month):
        return self.employee.get_unpaid_commission(month=month, year=2026)

    def pay(self, amount, month):
        with mock.patch('panel.views.calculate_company_balance', return_value=Decimal('1e9')):
            return self.client.post(reverse('panel:commission_pay', args=[self.employee.pk]), {
                'amount': amount, 'period_month': month, 'period_year': 2026})

    def test_payment_clears_its_own_month(self):
        self.pay('500000', 9)
        self.assertEqual(self.unpaid(9), Decimal('0'))
        self.assertEqual(self.unpaid(6), Decimal('300000'))
        payment = CommissionPayment.objects.get()
        self.assertEqual(list(payment.commissions.all()),
                         [Commission.objects.get(sale=self.september)])

    def test_payment_over_the_months_unpaid_refused(self):
        self.pay('600000', 9)  # within the 800000 owed overall, but not September's
        self.assertFalse(CommissionPayment.objects.exists())
        self.assertEqual(self.unpaid(9), Decimal('500000'))

    def test_leftover_goes_to_oldest_other_month(self):
        CommissionPayment.objects.create(employee=self.employee, amount=Decimal('600000'),
                                         period_month=9, period_year=2026)
        self.assertEqual(self.unpaid(9), Decimal('0'))
        self.assertEqual(self.unpaid(6), Decimal('200000'))

    def test_paid_commission_is_pulled_by_laptops(self):
        commission = Commission.objects.get(sale=self.september)
        before = commission.sync_updated_at
        self.pay('500000', 9)
        commission.refresh_from_db()
        self.assertGreater(commission.sync_updated_at, before)


@override_settings(SYNC_ROLE='server')
class ReallocatePaymentsMigrationTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(name='Rep', commission_percentage=Decimal('10'))
        self.commissions = {}
        for month, total in ((5, '1000000'), (6, '3000000'), (9, '5000000')):
            sale = Sale.objects.create(employee=self.employee, total=Decimal(total),
                                       created_at=_local(2026, month, 10))
            self.commissions[month] = Commission.objects.create(
                employee=self.employee, sale=sale, amount=Decimal(total) / 10,
                created_at=sale.created_at)

    def _record(self, amount, paid, period=None):
        """A payment as the old FIFO code left it: ``paid`` maps month -> amount."""
        with apply_guard():  # skip save()'s distribution
            payment = CommissionPayment.objects.create(
                employee=self.employee, amount=Decimal(amount),
                period_month=period, period_year=2026 if period else None)
        for month, value in paid.items():
            c = self.commissions[month]
            c.paid_amount += Decimal(value)
            c.save()
            payment.commissions.add(c)
        return payment

    def _run(self):
        _run_migration('0012_reallocate_commission_payments', 'reallocate')

    def paid(self, month):
        self.commissions[month].refresh_from_db()
        return self.commissions[month].paid_amount

    def test_period_payment_moves_to_its_month(self):
        # FIFO put September's 500000 on May and June first, Sept got the rest.
        payment = self._record('500000', {5: '100000', 6: '300000', 9: '100000'}, period=9)
        self._run()
        self.assertEqual((self.paid(5), self.paid(6), self.paid(9)),
                         (Decimal('0'), Decimal('0'), Decimal('500000')))
        self.assertEqual(list(payment.commissions.all()), [self.commissions[9]])

    def test_payment_without_period_keeps_its_commissions(self):
        # Paid June before May's commission existed (it was filled in later).
        self._record('300000', {6: '300000'})
        self._run()
        self.assertEqual((self.paid(5), self.paid(6)), (Decimal('0'), Decimal('300000')))

    def test_inconsistent_employee_left_alone(self):
        self._record('500000', {5: '100000', 6: '300000', 9: '100000'}, period=9)
        Commission.objects.filter(pk=self.commissions[5].pk).update(paid_amount=Decimal('150000'))
        self._run()
        self.assertEqual((self.paid(5), self.paid(9)), (Decimal('150000'), Decimal('100000')))


class _CommissionPaymentPagesMixin:
    """A logged-in manager and a 10% employee with June (300000 commission)
    and September (500000) sales."""

    def setUp(self):
        User = get_user_model()
        self.manager_user = User.objects.create_user('boss', password='pw12345!x')
        self.manager_user.groups.add(Group.objects.get_or_create(name=MANAGER_GROUP)[0])
        self.client.force_login(self.manager_user)
        self.employee = Employee.objects.create(name='Rep', commission_percentage=Decimal('10'))
        self.june = Sale.objects.create(employee=self.employee, total=Decimal('3000000'),
                                        created_at=_local(2026, 6, 10))
        self.september = Sale.objects.create(employee=self.employee, total=Decimal('5000000'),
                                             created_at=_local(2026, 9, 10))
        self.employee.recalculate_commissions()

    def login_salesperson(self):
        rep = get_user_model().objects.create_user('rep', password='pw12345!x')
        rep.groups.add(Group.objects.get_or_create(name=SALESPERSON_GROUP)[0])
        self.client.force_login(rep)


@override_settings(SYNC_ROLE='server')
class CommissionPaymentEditDeleteTests(_CommissionPaymentPagesMixin, TestCase):
    """Editing or deleting an employee's commission payment takes exactly its
    own share back out of the commissions it paid."""

    def commission(self, sale):
        return Commission.objects.get(employee=self.employee, sale=sale)

    def unpaid(self, month):
        return self.employee.get_unpaid_commission(month=month, year=2026)

    def pay(self, amount, month):
        return CommissionPayment.objects.create(
            employee=self.employee, amount=Decimal(amount), period_month=month, period_year=2026)

    def edit(self, payment, amount, month, note='', balance='1e9'):
        with mock.patch('panel.views.calculate_company_balance', return_value=Decimal(balance)):
            return self.client.post(reverse('panel:commission_payment_edit', args=[payment.pk]), {
                'amount': amount, 'period_month': month, 'period_year': 2026, 'note': note})

    def delete(self, payment):
        return self.client.post(reverse('panel:commission_payment_delete', args=[payment.pk]))

    def split(self, payment):
        payment.refresh_from_db()
        return {sale_id: Decimal(v) for sale_id, v in payment.allocations.items()}

    # --- the recorded split ---------------------------------------------------

    def test_payment_records_its_split(self):
        payment = self.pay('500000', 9)
        self.assertEqual(self.split(payment), {str(self.september.sync_id): Decimal('500000')})

    def test_leftover_split_names_each_commission(self):
        payment = self.pay('600000', 9)
        self.assertEqual(self.split(payment), {str(self.september.sync_id): Decimal('500000'),
                                               str(self.june.sync_id): Decimal('100000')})

    # --- delete -------------------------------------------------------------

    def test_delete_restores_unpaid_and_company_balance(self):
        from finance.models import Currency
        from finance.views import calculate_company_balance
        sdg = Currency.objects.get(code='SDG')
        payment = self.pay('500000', 9)
        balance = calculate_company_balance(sdg)
        resp = self.delete(payment)
        self.assertRedirects(resp, reverse('panel:employee_detail', args=[self.employee.pk])
                             + '?month=9&year=2026', fetch_redirect_response=False)
        self.assertFalse(CommissionPayment.objects.exists())
        self.assertEqual(self.unpaid(9), Decimal('500000'))
        self.assertEqual(calculate_company_balance(sdg), balance + Decimal('500000'))

    def test_delete_takes_back_only_its_own_share(self):
        first = self.pay('200000', 9)
        self.pay('100000', 9)
        self.delete(first)
        self.assertEqual(self.commission(self.september).paid_amount, Decimal('100000'))
        self.assertEqual(self.unpaid(9), Decimal('400000'))

    def test_delete_of_leftover_payment_restores_both_months(self):
        payment = self.pay('600000', 9)  # 500000 to September, 100000 to June
        self.delete(payment)
        self.assertEqual((self.unpaid(6), self.unpaid(9)), (Decimal('300000'), Decimal('500000')))

    def test_delete_after_commission_shrank_never_goes_negative(self):
        payment = self.pay('500000', 9)
        Commission.objects.filter(sale=self.september).update(amount=Decimal('400000'))  # a return
        self.delete(payment)
        commission = self.commission(self.september)
        self.assertEqual((commission.paid_amount, commission.unpaid_amount),
                         (Decimal('0'), Decimal('400000')))

    def test_delete_requires_post(self):
        payment = self.pay('500000', 9)
        self.assertEqual(self.client.get(
            reverse('panel:commission_payment_delete', args=[payment.pk])).status_code, 405)
        self.assertTrue(CommissionPayment.objects.filter(pk=payment.pk).exists())

    def test_payment_made_before_splits_were_kept_is_taken_back_from_its_links(self):
        commission = self.commission(self.june)
        with apply_guard():  # as the old code left it: no split, just the link
            payment = CommissionPayment.objects.create(
                employee=self.employee, amount=Decimal('300000'), paid_at=_local(2026, 6, 20))
        Commission.objects.filter(pk=commission.pk).update(paid_amount=Decimal('300000'))
        payment.commissions.add(commission)
        self.delete(payment)
        self.assertEqual(self.unpaid(6), Decimal('300000'))

    # --- edit ---------------------------------------------------------------

    def test_edit_page_shows_the_most_it_can_pay(self):
        payment = self.pay('200000', 9)
        resp = self.client.get(reverse('panel:commission_payment_edit', args=[payment.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '500,000.00')  # 300000 still unpaid + its own 200000
        # Nothing was taken back by showing it.
        self.assertEqual(self.unpaid(9), Decimal('300000'))

    def test_edit_lower_amount(self):
        payment = self.pay('500000', 9)
        resp = self.edit(payment, '300000', 9)
        self.assertRedirects(resp, reverse('panel:employee_detail', args=[self.employee.pk])
                             + '?month=9&year=2026', fetch_redirect_response=False)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal('300000'))
        self.assertEqual(self.unpaid(9), Decimal('200000'))
        self.assertEqual(self.split(payment), {str(self.september.sync_id): Decimal('300000')})

    def test_edit_higher_amount_within_the_month(self):
        payment = self.pay('300000', 9)
        self.edit(payment, '500000', 9)
        self.assertEqual(self.unpaid(9), Decimal('0'))
        self.assertEqual(self.unpaid(6), Decimal('300000'))

    def test_edit_over_the_months_unpaid_refused(self):
        payment = self.pay('500000', 9)
        resp = self.edit(payment, '600000', 9)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.errors(resp), [
            'المبلغ أكبر من العمولة غير المدفوعة لفترة 2026-09 (500000.00).'])
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal('500000'))
        self.assertEqual((self.unpaid(6), self.unpaid(9)), (Decimal('300000'), Decimal('0')))

    def test_edit_increase_needs_company_balance(self):
        payment = self.pay('300000', 9)
        self.edit(payment, '500000', 9, balance='100000')  # the extra 200000 isn't there
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal('300000'))
        self.assertEqual(self.unpaid(9), Decimal('200000'))

    def test_edit_decrease_needs_no_balance(self):
        payment = self.pay('300000', 9)
        self.edit(payment, '100000', 9, balance='0')
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal('100000'))

    def test_edit_moves_payment_to_another_month(self):
        payment = self.pay('300000', 9)
        resp = self.edit(payment, '300000', 6)
        self.assertRedirects(resp, reverse('panel:employee_detail', args=[self.employee.pk])
                             + '?month=6&year=2026', fetch_redirect_response=False)
        self.assertEqual((self.unpaid(6), self.unpaid(9)), (Decimal('0'), Decimal('500000')))
        payment.refresh_from_db()
        self.assertEqual((payment.period_month, payment.period_year), (6, 2026))
        self.assertEqual(list(payment.commissions.all()), [self.commission(self.june)])

    def test_note_edit_leaves_the_split_alone(self):
        payment = self.pay('500000', 9)
        stamp = self.commission(self.september).sync_updated_at
        self.edit(payment, '500000', 9, note='cash')
        payment.refresh_from_db()
        self.assertEqual(payment.note, 'cash')
        self.assertEqual(self.commission(self.september).sync_updated_at, stamp)

    def test_note_edit_of_payment_without_period_keeps_it_that_way(self):
        commission = self.commission(self.june)
        with apply_guard():
            payment = CommissionPayment.objects.create(
                employee=self.employee, amount=Decimal('300000'), paid_at=_local(2026, 6, 20))
        Commission.objects.filter(pk=commission.pk).update(paid_amount=Decimal('300000'))
        payment.commissions.add(commission)
        self.edit(payment, '300000', 6, note='cash')  # the form shows June, its paid month
        payment.refresh_from_db()
        self.assertEqual((payment.note, payment.period_month), ('cash', None))
        self.assertEqual(self.unpaid(6), Decimal('0'))

    def test_invalid_amount_refused(self):
        payment = self.pay('500000', 9)
        for amount in ('0', '-5', 'abc'):
            resp = self.edit(payment, amount, 9)
            self.assertEqual(resp.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal('500000'))

    def errors(self, resp):
        return [str(m) for m in get_messages(resp.wsgi_request) if m.level_tag == 'error']

    # --- pages & access -----------------------------------------------------

    def test_detail_page_offers_edit_and_delete(self):
        payment = self.pay('500000', 9)
        resp = self.client.get(reverse('panel:employee_detail', args=[self.employee.pk]),
                               {'month': 9, 'year': 2026})
        self.assertContains(resp, reverse('panel:commission_payment_edit', args=[payment.pk]))
        self.assertContains(resp, reverse('panel:commission_payment_delete', args=[payment.pk]))

    def test_salesperson_cannot_edit_or_delete(self):
        payment = self.pay('500000', 9)
        self.login_salesperson()
        edit_url = reverse('panel:commission_payment_edit', args=[payment.pk])
        self.assertRedirects(self.client.get(edit_url), reverse('panel:sale_list'),
                             fetch_redirect_response=False)
        self.client.post(edit_url, {'amount': '1', 'period_month': 9, 'period_year': 2026})
        self.delete(payment)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal('500000'))
        self.assertEqual(self.unpaid(9), Decimal('0'))


@override_settings(SYNC_ROLE='server')
class ManagerCommissionPaymentEditDeleteTests(_CommissionPaymentPagesMixin, TestCase):
    """A manager earns 2% of their employees' sales: 100000 for September."""

    def setUp(self):
        super().setUp()
        self.boss = Manager.objects.create(name='Boss', commission_percentage=Decimal('2'))
        self.boss.employees.add(self.employee)

    def pay(self, amount, month=9):
        return ManagerCommissionPayment.objects.create(
            manager=self.boss, amount=Decimal(amount), period_month=month, period_year=2026)

    def edit(self, payment, amount, month=9, note='', balance='1e9'):
        with mock.patch('panel.views.calculate_company_balance', return_value=Decimal(balance)):
            return self.client.post(
                reverse('panel:manager_commission_payment_edit', args=[payment.pk]),
                {'amount': amount, 'period_month': month, 'period_year': 2026, 'note': note})

    def pay_view(self, amount, balance='1e9'):
        with mock.patch('panel.views.calculate_company_balance', return_value=Decimal(balance)):
            return self.client.post(reverse('panel:manager_commission_pay', args=[self.boss.pk]),
                                    {'amount': amount, 'period_month': 9, 'period_year': 2026})

    def sdg_balance(self):
        from finance.models import Currency
        from finance.views import calculate_company_balance
        return calculate_company_balance(Currency.objects.get(code='SDG'))

    def amount_of(self, payment):
        payment.refresh_from_db()
        return payment.amount

    def test_edit_within_unpaid(self):
        payment = self.pay('60000')
        resp = self.edit(payment, '100000', note='rest')
        self.assertRedirects(resp, reverse('panel:manager_detail', args=[self.boss.pk])
                             + '?month=9&year=2026', fetch_redirect_response=False)
        self.assertEqual(self.amount_of(payment), Decimal('100000'))
        self.assertEqual(payment.note, 'rest')

    def test_edit_over_unpaid_refused(self):
        payment = self.pay('60000')
        self.pay('30000')
        self.edit(payment, '70001')  # only 70000 is left besides the other payment
        self.assertEqual(self.amount_of(payment), Decimal('60000'))

    def test_edit_to_month_without_commission_refused(self):
        payment = self.pay('60000')
        self.edit(payment, '60000', month=7)
        payment.refresh_from_db()
        self.assertEqual((payment.period_month, payment.amount), (9, Decimal('60000')))

    def test_edit_moves_payment_to_another_month(self):
        payment = self.pay('60000')  # June's commission is 60000 too (2% of 3000000)
        self.edit(payment, '60000', month=6)
        payment.refresh_from_db()
        self.assertEqual(payment.period_month, 6)

    def test_edit_page_renders(self):
        payment = self.pay('60000')
        resp = self.client.get(reverse('panel:manager_commission_payment_edit', args=[payment.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '100,000.00')  # 40000 still unpaid + its own 60000

    def test_delete(self):
        payment = self.pay('60000')
        resp = self.client.post(reverse('panel:manager_commission_payment_delete', args=[payment.pk]))
        self.assertRedirects(resp, reverse('panel:manager_detail', args=[self.boss.pk])
                             + '?month=9&year=2026', fetch_redirect_response=False)
        self.assertFalse(ManagerCommissionPayment.objects.exists())

    def test_detail_page_offers_edit_and_delete(self):
        payment = self.pay('60000')
        resp = self.client.get(reverse('panel:manager_detail', args=[self.boss.pk]),
                               {'month': 9, 'year': 2026})
        self.assertContains(resp, reverse('panel:manager_commission_payment_edit', args=[payment.pk]))
        self.assertContains(resp, reverse('panel:manager_commission_payment_delete', args=[payment.pk]))

    def test_pay_view_still_checks_unpaid(self):
        self.pay_view('100001')
        self.assertFalse(ManagerCommissionPayment.objects.exists())
        self.pay_view('100000')
        self.assertEqual(ManagerCommissionPayment.objects.get().amount, Decimal('100000'))

    # --- a cost paid out of the company's SDG cash ----------------------------

    def test_payments_come_out_of_the_sdg_balance(self):
        from finance.models import Currency
        from finance.views import calculate_company_balance
        usd = Currency.objects.get(code='USD')
        before, usd_before = self.sdg_balance(), calculate_company_balance(usd)
        payment = self.pay('60000')
        self.assertEqual(self.sdg_balance(), before - Decimal('60000'))
        self.assertEqual(calculate_company_balance(usd), usd_before)
        self.client.post(reverse('panel:manager_commission_payment_delete', args=[payment.pk]))
        self.assertEqual(self.sdg_balance(), before)

    def test_edit_moves_the_balance_by_the_difference(self):
        payment = self.pay('60000')
        before = self.sdg_balance()
        self.edit(payment, '100000')
        self.assertEqual(self.sdg_balance(), before - Decimal('40000'))

    def test_pay_view_needs_company_balance(self):
        resp = self.pay_view('60000', balance='59999.99')
        self.assertFalse(ManagerCommissionPayment.objects.exists())
        self.assertEqual(
            [str(m) for m in get_messages(resp.wsgi_request) if m.level_tag == 'error'],
            ['الرصيد الحالي للجنيه السوداني (59999.99) غير كافٍ لتغطية العمولة (60000).'])
        self.pay_view('60000', balance='60000')
        self.assertEqual(ManagerCommissionPayment.objects.get().amount, Decimal('60000'))

    def test_edit_increase_needs_company_balance(self):
        payment = self.pay('60000')
        self.edit(payment, '100000', balance='39999.99')  # the extra 40000 isn't there
        self.assertEqual(self.amount_of(payment), Decimal('60000'))
        self.edit(payment, '100000', balance='40000')
        self.assertEqual(self.amount_of(payment), Decimal('100000'))

    def test_edit_decrease_needs_no_balance(self):
        payment = self.pay('60000')
        self.edit(payment, '10000', balance='0')
        self.assertEqual(self.amount_of(payment), Decimal('10000'))

    def test_salesperson_cannot_edit_or_delete(self):
        payment = self.pay('60000')
        self.login_salesperson()
        self.edit(payment, '1')
        self.client.post(reverse('panel:manager_commission_payment_delete', args=[payment.pk]))
        self.assertEqual(self.amount_of(payment), Decimal('60000'))


@override_settings(SYNC_ROLE='server')
class RecordAllocationsMigrationTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(name='Rep', commission_percentage=Decimal('10'))
        self.commissions = {}
        for month, total in ((6, '3000000'), (9, '5000000')):
            sale = Sale.objects.create(employee=self.employee, total=Decimal(total),
                                       created_at=_local(2026, month, 10))
            self.commissions[month] = Commission.objects.create(
                employee=self.employee, sale=sale, amount=Decimal(total) / 10,
                created_at=sale.created_at)

    def _record(self, amount, paid, period=None, when=None):
        """A payment as the old code left it: no split, ``paid`` month -> amount."""
        with apply_guard():
            payment = CommissionPayment.objects.create(
                employee=self.employee, amount=Decimal(amount), period_month=period,
                period_year=2026 if period else None, paid_at=when or _local(2026, 9, 20))
        for month, value in paid.items():
            c = self.commissions[month]
            c.paid_amount += Decimal(value)
            c.save()
            payment.commissions.add(c)
        return payment

    def _run(self):
        _run_migration('0014_record_commission_payment_allocations', 'record_allocations')

    def test_split_recorded_and_payment_then_deletes_exactly(self):
        legacy = self._record('300000', {6: '300000'}, when=_local(2026, 6, 20))
        september = self._record('200000', {9: '200000'}, period=9)
        self._run()
        legacy.refresh_from_db()
        september.refresh_from_db()
        june_sale, sept_sale = (str(self.commissions[m].sale.sync_id) for m in (6, 9))
        self.assertEqual(legacy.allocations, {june_sale: '300000.00'})
        self.assertEqual(september.allocations, {sept_sale: '200000.00'})
        september.delete()
        self.commissions[9].refresh_from_db()
        self.commissions[6].refresh_from_db()
        self.assertEqual((self.commissions[6].paid_amount, self.commissions[9].paid_amount),
                         (Decimal('300000'), Decimal('0')))

    def test_inconsistent_employee_keeps_empty_split(self):
        payment = self._record('200000', {9: '200000'}, period=9)
        Commission.objects.filter(pk=self.commissions[6].pk).update(paid_amount=Decimal('5000'))
        self._run()
        payment.refresh_from_db()
        self.assertEqual(payment.allocations, {})
        self.commissions[6].refresh_from_db()
        self.assertEqual(self.commissions[6].paid_amount, Decimal('5000'))


@override_settings(SYNC_ROLE='server')
class SaleDeleteOwesBackCommissionTests(TestCase):
    """A sale whose commission was already paid can still be deleted: the
    employee owes back what they were paid of it, which comes off their unpaid
    commission for the month it was paid for, below zero when nothing is left.
    The 10% employee has two September sales: 500 and 10000 of commission."""

    def setUp(self):
        User = get_user_model()
        self.manager_user = User.objects.create_user('boss', password='pw12345!x')
        self.manager_user.groups.add(Group.objects.get_or_create(name=MANAGER_GROUP)[0])
        self.client.force_login(self.manager_user)
        self.employee = Employee.objects.create(name='Rep', commission_percentage=Decimal('10'))
        self.small = self.sale('5000', day=1)
        self.big = self.sale('100000', day=10)

    def sale(self, total, day, month=9):
        sale = Sale.objects.create(employee=self.employee, total=Decimal(total),
                                   created_at=_local(2026, month, day))
        self.employee.recalculate_commissions()
        return sale

    def unpaid(self, month=9):
        return self.employee.get_unpaid_commission(month=month, year=2026)

    def pay(self, amount, month=9):
        return CommissionPayment.objects.create(
            employee=self.employee, amount=Decimal(amount), period_month=month, period_year=2026)

    def delete_sale(self, sale):
        return self.client.post(reverse('panel:sale_delete', args=[sale.pk]))

    def edit(self, payment, amount, note=''):
        with mock.patch('panel.views.calculate_company_balance', return_value=Decimal('1e9')):
            return self.client.post(reverse('panel:commission_payment_edit', args=[payment.pk]), {
                'amount': amount, 'period_month': 9, 'period_year': 2026, 'note': note})

    def sdg_balance(self):
        from finance.views import calculate_company_balance
        return calculate_company_balance(Currency.objects.get(code='SDG'))

    # --- what is owed back ----------------------------------------------------

    def test_paid_commission_comes_off_what_is_unpaid(self):
        payment = self.pay('500')  # the older sale's commission, in full
        self.assertEqual(self.unpaid(), Decimal('10000'))
        resp = self.delete_sale(self.small)
        self.assertRedirects(resp, reverse('panel:sale_list'), fetch_redirect_response=False)
        self.assertFalse(Sale.objects.filter(pk=self.small.pk).exists())
        self.assertEqual(self.unpaid(), Decimal('9500'))
        payment.refresh_from_db()
        self.assertEqual((payment.amount, payment.owed_back), (Decimal('500'), Decimal('500')))

    def test_nothing_left_unpaid_goes_below_zero(self):
        self.pay('10500')
        self.assertEqual(self.unpaid(), Decimal('0'))
        self.delete_sale(self.small)
        self.assertEqual(self.unpaid(), Decimal('-500'))

    def test_unpaid_part_simply_goes_with_the_sale(self):
        self.pay('200')  # 200 of the older sale's 500
        self.assertEqual(self.unpaid(), Decimal('10300'))
        self.delete_sale(self.small)
        self.assertEqual(self.unpaid(), Decimal('9800'))

    def test_other_months_untouched(self):
        june = self.sale('30000', day=10, month=6)
        self.pay('3000', month=6)
        self.delete_sale(june)
        self.assertEqual((self.unpaid(6), self.unpaid(9)), (Decimal('-3000'), Decimal('10500')))

    def test_money_paid_out_stays_paid_out(self):
        self.pay('500')
        before = self.sdg_balance()
        self.delete_sale(self.small)
        self.assertEqual(self.sdg_balance(), before)

    def test_commission_paid_by_no_payment_is_not_owed_back(self):
        # Marked paid with no payment behind it: no money went out for it.
        Commission.objects.filter(sale=self.small).update(paid_amount=Decimal('500'))
        self.delete_sale(self.small)
        self.assertFalse(Sale.objects.filter(pk=self.small.pk).exists())
        self.assertEqual(self.unpaid(), Decimal('10000'))

    def test_customer_payment_still_blocks_delete(self):
        invoice = Invoice.objects.create(sale=self.small, total=Decimal('5000'))
        InvoicePayment.objects.create(invoice=invoice, amount=Decimal('1000'))
        self.pay('500')
        self.delete_sale(self.small)
        self.assertTrue(Sale.objects.filter(pk=self.small.pk).exists())
        self.assertEqual(CommissionPayment.objects.get().owed_back, Decimal('0'))

    # --- the payment afterwards -----------------------------------------------

    def test_what_is_owed_back_caps_the_next_payment(self):
        self.pay('500')
        self.delete_sale(self.small)
        url = reverse('panel:commission_pay', args=[self.employee.pk])
        with mock.patch('panel.views.calculate_company_balance', return_value=Decimal('1e9')):
            self.client.post(url, {'amount': '10000', 'period_month': 9, 'period_year': 2026})
            self.assertEqual(CommissionPayment.objects.count(), 1)
            self.client.post(url, {'amount': '9500', 'period_month': 9, 'period_year': 2026})
        self.assertEqual(self.unpaid(), Decimal('0'))

    def test_deleting_the_payment_cancels_what_is_owed_back(self):
        payment = self.pay('500')
        self.delete_sale(self.small)
        before = self.sdg_balance()
        self.client.post(reverse('panel:commission_payment_delete', args=[payment.pk]))
        self.assertEqual(self.unpaid(), Decimal('10000'))
        self.assertEqual(self.sdg_balance(), before + Decimal('500'))
        self.assertEqual(Commission.objects.get(sale=self.big).paid_amount, Decimal('0'))

    def test_edited_payment_pays_what_is_left(self):
        payment = self.pay('500')
        self.delete_sale(self.small)
        self.edit(payment, '400')
        payment.refresh_from_db()
        self.assertEqual((payment.allocations, payment.cancelled),
                         ({str(self.big.sync_id): '400.00'}, {}))
        self.assertEqual(self.unpaid(), Decimal('9600'))

    def test_note_edit_keeps_what_is_owed_back(self):
        payment = self.pay('500')
        self.delete_sale(self.small)
        self.edit(payment, '500', note='cash')
        payment.refresh_from_db()
        self.assertEqual((payment.note, payment.owed_back), ('cash', Decimal('500')))
        self.assertEqual(self.unpaid(), Decimal('9500'))

    def test_edit_page_room_leaves_out_its_own_debt(self):
        payment = self.pay('500')
        self.delete_sale(self.small)
        resp = self.client.get(reverse('panel:commission_payment_edit', args=[payment.pk]))
        self.assertContains(resp, '10,000.00')  # all of the other sale's
        self.assertEqual(self.unpaid(), Decimal('9500'))  # showing it changed nothing

    def test_payment_from_before_splits_were_kept_is_owed_back_too(self):
        # As the old code left it: no split, just linked to what it paid.
        with apply_guard():
            payment = CommissionPayment.objects.create(
                employee=self.employee, amount=Decimal('600'), paid_at=_local(2026, 9, 20))
        for sale, paid in ((self.small, '500'), (self.big, '100')):
            commission = Commission.objects.get(sale=sale)
            Commission.objects.filter(pk=commission.pk).update(paid_amount=Decimal(paid))
            payment.commissions.add(commission)
        self.delete_sale(self.small)
        payment.refresh_from_db()
        self.assertEqual(payment.owed_back, Decimal('500'))
        self.assertEqual(self.unpaid(), Decimal('9400'))
        # Deleting it now takes back only what it paid of the sale still there.
        self.client.post(reverse('panel:commission_payment_delete', args=[payment.pk]))
        self.assertEqual(Commission.objects.get(sale=self.big).paid_amount, Decimal('0'))
        self.assertEqual(self.unpaid(), Decimal('10000'))

    # --- pages ----------------------------------------------------------------

    def test_confirmation_page_tells_what_is_owed_back(self):
        self.pay('500')
        resp = self.client.get(reverse('panel:sale_delete', args=[self.small.pk]))
        self.assertContains(resp, 'عمولة هذه الفاتورة مدفوعة للمندوب Rep (500.00)')
        self.assertNotContains(resp, 'لا يمكن حذف')
        resp = self.delete_sale(self.small)
        self.assertEqual(
            [str(m) for m in get_messages(resp.wsgi_request) if m.level_tag == 'warning'],
            ['خُصم 500.00 من عمولة المندوب Rep غير المدفوعة، لأن عمولة الفاتورة المحذوفة كانت مدفوعة له.'])

    def test_salesperson_sees_no_commission_but_it_is_still_owed_back(self):
        self.pay('500')
        rep = get_user_model().objects.create_user('rep', password='pw12345!x')
        rep.groups.add(Group.objects.get_or_create(name=SALESPERSON_GROUP)[0])
        self.client.force_login(rep)
        resp = self.client.get(reverse('panel:sale_delete', args=[self.small.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'مدفوعة للمندوب')
        self.delete_sale(self.small)
        self.assertEqual(self.unpaid(), Decimal('9500'))

    def test_employee_page_explains_what_is_owed_back(self):
        self.pay('10500')
        self.delete_sale(self.small)
        resp = self.client.get(reverse('panel:employee_detail', args=[self.employee.pk]),
                               {'month': 9, 'year': 2026})
        self.assertContains(resp, 'منها 500.00 عن عمولات أُلغيت')
        self.assertContains(resp, '(مستحق على المندوب)')

    def test_manager_commission_follows_the_deleted_sale(self):
        from panel.views import _manager_unpaid_commission
        boss = Manager.objects.create(name='Boss', commission_percentage=Decimal('2'))
        boss.employees.add(self.employee)
        ManagerCommissionPayment.objects.create(  # 2% of 105000: all of it
            manager=boss, amount=Decimal('2100'), period_month=9, period_year=2026)
        resp = self.client.get(reverse('panel:sale_delete', args=[self.small.pk]))
        self.assertContains(resp, 'تنقص عمولة المدير Boss لشهر 2026-09 بمقدار 100.00')
        self.delete_sale(self.small)
        self.assertEqual(_manager_unpaid_commission(boss, 9, 2026), Decimal('-100'))

    # --- sync -----------------------------------------------------------------

    @override_settings(SYNC_ROLE='manager')
    def test_laptop_queues_the_payment_with_the_deletion(self):
        payment = self.pay('500')
        commission = Commission.objects.get(sale=self.small)
        SyncOutbox.objects.all().delete()
        self.delete_sale(self.small)
        queued = {(e.model_label, str(e.sync_id), e.deleted) for e in SyncOutbox.objects.all()}
        self.assertIn(('panel.CommissionPayment', str(payment.sync_id), False), queued)
        self.assertIn(('panel.Commission', str(commission.sync_id), True), queued)
        self.assertIn(('panel.Sale', str(self.small.sync_id), True), queued)


@override_settings(SYNC_ROLE='server')
class ReturnOwesBackCommissionTests(SaleFormTestMixin, TestCase):
    """A return taking a commission below what it was paid: the employee owes
    the difference back, and removing the return undoes it. The 10% employee
    sold 3 units at 2000: 600 of commission."""

    def setUp(self):
        super().setUp()
        self.employee = Employee.objects.create(name='Rep', commission_percentage=Decimal('10'))
        data = self._data([self._row(self.inv_a, 3)])
        data['employee'] = self.employee.pk
        self.client.post(reverse('panel:sale_create'), data)
        self.sale = Sale.objects.get()
        self.item = self.sale.items.get()
        local = localtime(self.sale.created_at)
        self.month, self.year = local.month, local.year

    def commission(self):
        commission = Commission.objects.get(employee=self.employee, sale=self.sale)
        return commission.amount, commission.paid_amount

    def unpaid(self):
        return self.employee.get_unpaid_commission(self.month, self.year)

    def pay(self, amount):
        return CommissionPayment.objects.create(
            employee=self.employee, amount=Decimal(amount),
            period_month=self.month, period_year=self.year)

    def owed_back(self, payment):
        payment.refresh_from_db()
        return payment.owed_back

    def return_units(self, quantity):
        return self.client.post(reverse('panel:sale_return_product', args=[self.sale.pk]),
                                {'sale_item': self.item.pk, 'quantity': quantity, 'note': ''})

    def warnings(self, resp):
        return [str(m) for m in get_messages(resp.wsgi_request) if m.level_tag == 'warning']

    def test_return_on_a_paid_commission_is_owed_back(self):
        payment = self.pay('600')
        self.return_units(1)  # 400 of commission now
        self.assertEqual(self.commission(), (Decimal('400'), Decimal('400')))
        self.assertEqual(self.owed_back(payment), Decimal('200'))
        self.assertEqual(self.unpaid(), Decimal('-200'))

    def test_unpaid_part_takes_the_return_first(self):
        payment = self.pay('300')
        self.return_units(1)  # 400: still more than was paid
        self.assertEqual(self.owed_back(payment), Decimal('0'))
        self.assertEqual(self.unpaid(), Decimal('100'))

    def test_only_what_was_paid_beyond_the_new_amount(self):
        payment = self.pay('500')
        self.return_units(1)
        self.assertEqual(self.owed_back(payment), Decimal('100'))
        self.assertEqual(self.unpaid(), Decimal('-100'))

    def test_newest_payment_owes_it_back_first(self):
        first, second = self.pay('400'), self.pay('200')
        self.return_units(1)
        self.assertEqual((self.owed_back(first), self.owed_back(second)), (Decimal('0'), Decimal('200')))

    def test_removing_the_return_undoes_it(self):
        payment = self.pay('600')
        self.return_units(1)
        ReturnedProduct.objects.get().delete()
        self.assertEqual(self.commission(), (Decimal('600'), Decimal('600')))
        self.assertEqual((self.owed_back(payment), self.unpaid()), (Decimal('0'), Decimal('0')))

    def test_removing_one_of_two_returns(self):
        payment = self.pay('600')
        self.return_units(1)
        self.return_units(1)  # 200 left of the 600 paid
        self.assertEqual(self.owed_back(payment), Decimal('400'))
        ReturnedProduct.objects.order_by('pk').first().delete()
        self.assertEqual(self.commission(), (Decimal('400'), Decimal('400')))
        self.assertEqual((self.owed_back(payment), self.unpaid()), (Decimal('200'), Decimal('-200')))

    def test_older_overpayment_stays_as_it_was(self):
        payment = self.pay('600')
        with mock.patch('panel.models._settle_paid_amount'):
            self.return_units(1)  # as the old code left it: 400, with 600 paid
        self.assertEqual(self.commission(), (Decimal('400'), Decimal('600')))
        self.return_units(1)  # 200: only this return's 200 is owed back
        self.assertEqual(self.owed_back(payment), Decimal('200'))
        self.assertEqual(self.unpaid(), Decimal('-200'))
        ReturnedProduct.objects.order_by('pk').last().delete()
        self.assertEqual(self.commission(), (Decimal('400'), Decimal('600')))
        self.assertEqual(self.owed_back(payment), Decimal('0'))

    def test_deleting_the_payment_after_a_return(self):
        payment = self.pay('600')
        self.return_units(1)
        self.client.post(reverse('panel:commission_payment_delete', args=[payment.pk]))
        self.assertEqual(self.commission(), (Decimal('400'), Decimal('0')))
        self.assertEqual(self.unpaid(), Decimal('400'))

    def test_deleting_the_payment_that_owes_it_back_keeps_the_others(self):
        first, second = self.pay('400'), self.pay('200')
        self.return_units(1)  # the newer one owes its 200 back
        self.client.post(reverse('panel:commission_payment_delete', args=[second.pk]))
        self.assertEqual(self.commission(), (Decimal('400'), Decimal('400')))  # the older one's
        self.assertEqual((self.owed_back(first), self.unpaid()), (Decimal('0'), Decimal('0')))

    def test_editing_the_payment_after_a_return(self):
        payment = self.pay('600')
        self.return_units(1)
        with mock.patch('panel.views.calculate_company_balance', return_value=Decimal('1e9')):
            self.client.post(reverse('panel:commission_payment_edit', args=[payment.pk]), {
                'amount': '400', 'period_month': self.month, 'period_year': self.year})
        payment.refresh_from_db()
        self.assertEqual((payment.amount, payment.owed_back), (Decimal('400'), Decimal('0')))
        self.assertEqual(self.commission(), (Decimal('400'), Decimal('400')))
        self.assertEqual(self.unpaid(), Decimal('0'))

    def test_deleting_the_sale_after_a_return_owes_back_the_rest(self):
        payment = self.pay('600')
        self.return_units(1)
        self.client.post(reverse('panel:sale_delete', args=[self.sale.pk]))
        self.assertFalse(Sale.objects.filter(pk=self.sale.pk).exists())
        self.assertEqual(self.owed_back(payment), Decimal('600'))
        self.assertEqual(self.unpaid(), Decimal('-600'))

    def test_manager_is_told_what_the_return_charged(self):
        self.pay('600')
        message = 'خُصم 200.00 من عمولة المندوب Rep غير المدفوعة، لأن عمولة الفاتورة كانت مدفوعة له قبل الإرجاع.'
        self.assertEqual(self.warnings(self.return_units(1)), [message])
        self.client.get(reverse('panel:sale_detail', args=[self.sale.pk]))  # shows it, done
        self.assertEqual(self.warnings(self.return_units(1)), [message])  # the next one's own

    def test_nothing_said_when_nothing_is_charged(self):
        self.pay('300')
        self.assertEqual(self.warnings(self.return_units(1)), [])

    def test_salesperson_return_is_charged_without_showing_it(self):
        self.pay('600')
        rep = get_user_model().objects.create_user('rep', password='pw12345!x')
        rep.groups.add(Group.objects.get_or_create(name=SALESPERSON_GROUP)[0])
        self.client.force_login(rep)
        self.assertEqual(self.warnings(self.return_units(1)), [])
        self.assertEqual(self.unpaid(), Decimal('-200'))

    def test_manager_commission_follows_the_return(self):
        from panel.views import _manager_unpaid_commission
        boss = Manager.objects.create(name='Boss', commission_percentage=Decimal('2'))
        boss.employees.add(self.employee)
        ManagerCommissionPayment.objects.create(  # 2% of 6000: all of it
            manager=boss, amount=Decimal('120'), period_month=self.month, period_year=self.year)
        self.return_units(1)
        self.assertEqual(_manager_unpaid_commission(boss, self.month, self.year), Decimal('-40'))

    @override_settings(SYNC_ROLE='manager')
    def test_laptop_queues_the_payment_with_the_return(self):
        payment = self.pay('600')
        SyncOutbox.objects.all().delete()
        self.return_units(1)
        queued = {(e.model_label, str(e.sync_id), e.deleted) for e in SyncOutbox.objects.all()}
        commission = Commission.objects.get(sale=self.sale)
        self.assertIn(('panel.CommissionPayment', str(payment.sync_id), False), queued)
        self.assertIn(('panel.Commission', str(commission.sync_id), False), queued)
