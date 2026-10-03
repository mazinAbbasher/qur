"""Sync engine + API tests — the reliability guarantees, proven.

Covers: token auth, role-based push rejection, field stripping on pull,
idempotent upserts (no duplicates), FK-by-sync_id resolution regardless of
order, oversell protection, tombstones, and no-clobber-on-pull.
"""

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from finance.models import Currency, CurrencyExchange, Partner, PartnerTransaction
from panel.models import (
    Client, Commission, Employee, Expense, Inventory, Invoice, InvoicePayment,
    Product, Sale, SaleItem, Shipment, ReturnedProduct,
)
from sync import registry
from sync.client import _clamped_cursor, pull as client_pull
from sync.engine import apply_batch
from sync.models import Node, SyncConflict, SyncOutbox, SyncState
from sync.serializers import serialize_instance


def row(label, sync_id, deleted=False, **fields):
    return {'label': label, 'sync_id': str(sync_id),
            'is_deleted': deleted, 'fields': fields}


# Pin the role so tests are deterministic regardless of the machine's .env
# (this box is configured as a live salesperson node). 'server' = tracking off.
@override_settings(SYNC_ROLE='server')
class SyncSetup(TestCase):
    def setUp(self):
        self.sales_node = Node.objects.create(
            name='rep-1', role='salesperson', token='tok-sales')
        self.mgr_node = Node.objects.create(
            name='mgr-1', role='manager', token='tok-mgr')

        self.product = Product.objects.create(name='Med A', exchange_rate=600)
        self.shipment = Shipment.objects.create(
            product=self.product, quantity=5, shipment_cost=Decimal('10'),
            cost_usd=Decimal('2'), cost_sdg=Decimal('1200'),
            sale_sdg=Decimal('2000'), batch_number='B1',
            expiry_date=date(2030, 1, 1), exchange_rate=600,
        )
        self.inventory = Inventory.objects.create(
            product=self.product, shipment=self.shipment, quantity=5)
        self.employee = Employee.objects.create(name='Rep One')

    def push(self, token, changes):
        return self.client.post(
            reverse('sync:api_push'),
            data=json.dumps({'changes': changes}),
            content_type='application/json',
            HTTP_X_SYNC_TOKEN=token,
        )

    def pull(self, token, since=None, **body):
        return self.client.post(
            reverse('sync:api_pull'),
            data=json.dumps({'since': since, **body}),
            content_type='application/json',
            HTTP_X_SYNC_TOKEN=token,
        )


class AuthTests(SyncSetup):
    def test_missing_token_rejected(self):
        resp = self.client.post(reverse('sync:api_pull'),
                                data=json.dumps({}), content_type='application/json')
        self.assertEqual(resp.status_code, 401)

    def test_bad_token_rejected(self):
        resp = self.pull('nope')
        self.assertEqual(resp.status_code, 401)

    def test_valid_token_ok(self):
        resp = self.pull('tok-mgr')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])


class RolePolicyTests(SyncSetup):
    def test_salesperson_cannot_push_shipment(self):
        before = Shipment.objects.count()
        resp = self.push('tok-sales', [
            row('panel.Shipment', uuid.uuid4(), product=str(self.product.sync_id),
                quantity=9, shipment_cost='1', batch_number='HACK',
                expiry_date='2031-01-01'),
        ])
        self.assertEqual(resp.status_code, 200)
        self.assertIn('panel.Shipment', resp.json()['rejected'])
        self.assertEqual(Shipment.objects.count(), before)  # nothing created

    def test_salesperson_cannot_push_product(self):
        # Products are view-only for reps; the server must reject their pushes
        # (this is the 'rejected [panel.Product]' note in the sync log).
        resp = self.push('tok-sales', [
            row('panel.Product', self.product.sync_id, name='Hacked', exchange_rate=1),
        ])
        self.assertEqual(resp.status_code, 200)
        self.assertIn('panel.Product', resp.json()['rejected'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Med A')  # unchanged

    def test_manager_can_push_product(self):
        resp = self.push('tok-mgr', [
            row('panel.Product', self.product.sync_id, name='Med A', exchange_rate=700),
        ])
        self.assertEqual(resp.json()['applied'], 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.exchange_rate, 700)

    def test_manager_can_push_shipment(self):
        resp = self.push('tok-mgr', [
            row('panel.Shipment', uuid.uuid4(), product=str(self.product.sync_id),
                quantity=9, shipment_cost='1', batch_number='OK',
                expiry_date='2031-01-01'),
        ])
        self.assertEqual(resp.json()['applied'], 1)
        self.assertTrue(Shipment.objects.filter(batch_number='OK').exists())

    def test_pull_strips_costs_for_salesperson(self):
        data = self.pull('tok-sales').json()
        shipments = [r for r in data['changes'] if r['label'] == 'panel.Shipment']
        self.assertTrue(shipments)
        for s in shipments:
            for hidden in ('cost_usd', 'cost_sdg', 'shipment_cost', 'supplier'):
                self.assertNotIn(hidden, s['fields'])
            self.assertIn('sale_sdg', s['fields'])  # sale price still sent

    def test_pull_includes_costs_for_manager(self):
        data = self.pull('tok-mgr').json()
        shipments = [r for r in data['changes'] if r['label'] == 'panel.Shipment']
        self.assertTrue(any('cost_usd' in s['fields'] for s in shipments))


class ExpensePolicyTests(SyncSetup):
    """Daily expenses: a rep authors and pushes their own, and the server/
    manager receives them — but they are never sent back down to a salesperson,
    so the company-wide expense total stays private on a rep laptop."""

    def test_salesperson_can_push_expense(self):
        uid = uuid.uuid4()
        resp = self.push('tok-sales', [
            row('panel.Expense', uid, description='Taxi', amount='500',
                date='2026-08-10'),
        ])
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('panel.Expense', resp.json()['rejected'])
        self.assertEqual(resp.json()['applied'], 1)
        exp = Expense.objects.get(sync_id=uid)
        self.assertEqual(exp.description, 'Taxi')
        self.assertEqual(exp.amount, Decimal('500'))

    def test_manager_can_push_expense(self):
        uid = uuid.uuid4()
        resp = self.push('tok-mgr', [
            row('panel.Expense', uid, description='Rent', amount='9000',
                date='2026-08-01'),
        ])
        self.assertEqual(resp.json()['applied'], 1)
        self.assertTrue(Expense.objects.filter(sync_id=uid).exists())

    def test_pull_hides_expenses_from_salesperson(self):
        Expense.objects.create(description='Rent', amount=Decimal('9000'),
                               date=date(2026, 8, 1))
        data = self.pull('tok-sales').json()
        self.assertFalse([r for r in data['changes']
                          if r['label'] == 'panel.Expense'])

    def test_pull_sends_expenses_to_manager(self):
        Expense.objects.create(description='Rent', amount=Decimal('9000'),
                               date=date(2026, 8, 1))
        data = self.pull('tok-mgr').json()
        self.assertTrue([r for r in data['changes']
                         if r['label'] == 'panel.Expense'])


class CurrencyExchangePolicyTests(SyncSetup):
    """A rep's exchange reaches the server only through api_currency_exchange,
    which refuses it unless the company holds enough of the sold currency. The
    company's exchange history is never sent down to a rep."""

    def setUp(self):
        super().setUp()
        self.sdg = Currency.objects.get(code='SDG')
        self.usd = Currency.objects.get(code='USD')

    def exchange_row(self, uid):
        return row('finance.CurrencyExchange', uid,
                   sold_currency=str(self.sdg.sync_id),
                   bought_currency=str(self.usd.sync_id),
                   sold_amount='600000', bought_amount='100',
                   exchange_rate='6000', date='2026-09-01')

    def post_exchange(self, token, exchange):
        return self.client.post(
            reverse('sync:api_currency_exchange'),
            data=json.dumps({'row': exchange}),
            content_type='application/json',
            HTTP_X_SYNC_TOKEN=token,
        )

    def deposit_sdg(self, amount):
        PartnerTransaction.objects.create(
            partner=Partner.objects.create(full_name='P'), transaction_type='deposit',
            amount=Decimal(amount), currency=self.sdg)

    def test_salesperson_cannot_push_exchange(self):
        # Push would skip the balance check, so it's closed to reps.
        self.deposit_sdg('1000000')
        uid = uuid.uuid4()
        resp = self.push('tok-sales', [self.exchange_row(uid)])
        self.assertIn('finance.CurrencyExchange', resp.json()['rejected'])
        self.assertFalse(CurrencyExchange.objects.filter(sync_id=uid).exists())

    def test_endpoint_refuses_short_balance_without_revealing_it(self):
        self.deposit_sdg('12345')
        uid = uuid.uuid4()
        resp = self.post_exchange('tok-sales', self.exchange_row(uid))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json(), {'ok': False, 'error': 'insufficient_balance'})
        self.assertFalse(CurrencyExchange.objects.filter(sync_id=uid).exists())

    def test_endpoint_records_exchange_when_balance_enough(self):
        self.deposit_sdg('1000000')
        uid = uuid.uuid4()
        resp = self.post_exchange('tok-sales', self.exchange_row(uid))
        self.assertEqual(resp.status_code, 200)
        ex = CurrencyExchange.objects.get(sync_id=uid)
        self.assertEqual(ex.sold_currency, self.sdg)
        self.assertEqual(ex.bought_amount, Decimal('100'))
        # A retry (laptop lost the reply) is accepted without a duplicate,
        # even though the balance no longer covers a second exchange.
        resp = self.post_exchange('tok-sales', self.exchange_row(uid))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(CurrencyExchange.objects.filter(sync_id=uid).count(), 1)

    def test_second_exchange_cannot_spend_the_same_balance(self):
        self.deposit_sdg('1000000')
        self.assertEqual(self.post_exchange('tok-sales', self.exchange_row(uuid.uuid4())).status_code, 200)
        self.assertEqual(self.post_exchange('tok-sales', self.exchange_row(uuid.uuid4())).status_code, 409)
        self.assertEqual(CurrencyExchange.objects.count(), 1)

    def test_endpoint_requires_token(self):
        resp = self.post_exchange('nope', self.exchange_row(uuid.uuid4()))
        self.assertEqual(resp.status_code, 401)

    def test_pull_hides_exchanges_from_salesperson(self):
        CurrencyExchange.objects.create(
            sold_currency=Currency.objects.get(code='SDG'),
            bought_currency=Currency.objects.get(code='USD'),
            sold_amount=Decimal('600000'), bought_amount=Decimal('100'),
            exchange_rate=Decimal('6000'))
        rep = self.pull('tok-sales').json()
        self.assertFalse([r for r in rep['changes']
                          if r['label'] == 'finance.CurrencyExchange'])
        mgr = self.pull('tok-mgr').json()
        self.assertTrue([r for r in mgr['changes']
                         if r['label'] == 'finance.CurrencyExchange'])


class ApplyEngineTests(SyncSetup):
    def _sale_batch(self, sale_uid, client_uid, item_uid, qty=2):
        # Deliberately shuffled (child before parent) to prove ordering works.
        return [
            row('panel.SaleItem', item_uid, sale=str(sale_uid),
                inventory=str(self.inventory.sync_id), quantity=qty, price='2000'),
            row('panel.Sale', sale_uid, client=str(client_uid),
                employee=str(self.employee.sync_id),
                created_at='2026-01-01T10:00:00', total='4000'),
            row('panel.Client', client_uid, name='New Client'),
        ]

    def test_fk_resolution_and_ordering(self):
        sale_uid, client_uid, item_uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        resp = self.push('tok-sales', self._sale_batch(sale_uid, client_uid, item_uid))
        self.assertTrue(resp.json()['ok'])
        sale = Sale.objects.get(sync_id=sale_uid)
        item = SaleItem.objects.get(sync_id=item_uid)
        self.assertEqual(item.sale_id, sale.id)
        self.assertEqual(sale.client.name, 'New Client')
        self.assertEqual(item.inventory_id, self.inventory.id)

    def test_idempotent_no_duplicates(self):
        sale_uid, client_uid, item_uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        batch = self._sale_batch(sale_uid, client_uid, item_uid)
        self.push('tok-sales', batch)
        second = self.push('tok-sales', batch).json()
        self.assertEqual(Sale.objects.filter(sync_id=sale_uid).count(), 1)
        self.assertEqual(SaleItem.objects.filter(sync_id=item_uid).count(), 1)
        self.assertEqual(second['applied'], 0)      # nothing changed
        self.assertGreaterEqual(second['noop'], 3)

    def test_oversell_is_clamped_and_logged(self):
        sale_uid, client_uid, item_uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # inventory/shipment stock is 5; sell 10.
        resp = self.push('tok-sales',
                         self._sale_batch(sale_uid, client_uid, item_uid, qty=10))
        self.assertTrue(resp.json()['ok'])
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 0)          # clamped, never negative
        self.assertTrue(SyncConflict.objects.filter(reason='oversell').exists())

    def test_normal_sale_deducts_stock(self):
        sale_uid, client_uid, item_uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.push('tok-sales', self._sale_batch(sale_uid, client_uid, item_uid, qty=2))
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 3)          # 5 - 2

    def test_tombstone_hard_deletes(self):
        # The app doesn't filter is_deleted, so a delete must actually remove
        # the row (no ghost rows in list views).
        client = Client.objects.create(name='ToDelete')
        sid = client.sync_id
        self.push('tok-mgr', [row('panel.Client', sid, deleted=True)])
        self.assertFalse(Client.objects.filter(sync_id=sid).exists())

    def test_deleting_sale_restores_stock(self):
        sale_uid, client_uid, item_uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.push('tok-sales', self._sale_batch(sale_uid, client_uid, item_uid, qty=2))
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 3)          # 5 - 2

        # Delete the sale; its items cascade and stock returns to 5.
        self.push('tok-sales', [row('panel.Sale', sale_uid, deleted=True)])
        self.assertFalse(Sale.objects.filter(sync_id=sale_uid).exists())
        self.assertFalse(SaleItem.objects.filter(sync_id=item_uid).exists())
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 5)

    def test_tombstone_for_unknown_row_is_noop(self):
        resp = self.push('tok-mgr', [row('panel.Client', uuid.uuid4(), deleted=True)])
        self.assertTrue(resp.json()['ok'])
        self.assertEqual(resp.json()['applied'], 0)

    def test_deletion_propagates_via_pull(self):
        # A delete pushed by one node must reach others through pull.
        client = Client.objects.create(name='Shared')
        sid = str(client.sync_id)
        self.push('tok-mgr', [row('panel.Client', sid, deleted=True)])
        self.assertFalse(Client.objects.filter(sync_id=sid).exists())

        # A manager pulling from scratch is told about the deletion.
        data = self.pull('tok-mgr').json()
        tombs = [r for r in data['changes']
                 if r['sync_id'] == sid and r['is_deleted']]
        self.assertEqual(len(tombs), 1)


class PullConflictTests(SyncSetup):
    def test_pull_does_not_clobber_pending_local_change(self):
        client = Client.objects.create(name='Local Name')
        # Simulate an un-pushed local edit.
        SyncOutbox.objects.create(model_label='panel.Client', sync_id=client.sync_id)
        incoming = serialize_instance(client)
        incoming['fields']['name'] = 'Server Name'  # server has a different value

        stats = apply_batch([incoming], is_pull=True, node_name='rep-1')

        client.refresh_from_db()
        self.assertEqual(client.name, 'Local Name')   # local kept
        self.assertEqual(stats['conflicts'], 1)
        self.assertTrue(SyncConflict.objects.filter(reason='stale_write').exists())


class CursorClampTests(SyncSetup):
    """Gap (a): a row we can't apply must not be skipped by the pull cursor."""

    def _orphan_saleitem(self, ts):
        # A SaleItem whose parent Sale is absent -> unresolved after retry.
        r = row('panel.SaleItem', uuid.uuid4(), sale=str(uuid.uuid4()),
                inventory=str(self.inventory.sync_id), quantity=1, price='2000')
        r['sync_updated_at'] = ts
        return r

    def test_missing_reference_reports_deferred_min(self):
        ts = '2026-05-01T12:00:00+00:00'
        stats = apply_batch([self._orphan_saleitem(ts)], is_pull=True, node_name='rep-1')
        self.assertEqual(stats['applied'], 0)
        self.assertEqual(stats['deferred_min'], parse_datetime(ts))
        self.assertEqual(
            SyncConflict.objects.filter(reason='missing_reference').count(), 1)

    def test_missing_reference_logged_once_across_retries(self):
        r = self._orphan_saleitem('2026-05-01T12:00:00+00:00')
        apply_batch([r], is_pull=True, node_name='rep-1')
        apply_batch([r], is_pull=True, node_name='rep-1')  # re-delivered next pull
        self.assertEqual(
            SyncConflict.objects.filter(reason='missing_reference').count(), 1)

    def test_clamped_cursor_stays_before_unapplied_row(self):
        server_cursor = parse_datetime('2026-05-01T12:00:05+00:00')
        deferred_min = parse_datetime('2026-05-01T12:00:00+00:00')
        self.assertLess(_clamped_cursor(server_cursor, deferred_min), deferred_min)

    def test_clamped_cursor_unchanged_without_deferral(self):
        server_cursor = parse_datetime('2026-05-01T12:00:05+00:00')
        self.assertEqual(_clamped_cursor(server_cursor, None), server_cursor)


class PushCollisionTests(SyncSetup):
    """Gap (b): a stale push over a shared record is applied but audited."""

    def test_stale_push_wins_but_is_logged(self):
        client = Client.objects.create(name='Server Name')
        older = (client.sync_updated_at - timedelta(minutes=5)).isoformat()
        incoming = row('panel.Client', client.sync_id, name='Rep Old Edit')
        incoming['sync_updated_at'] = older

        stats = apply_batch([incoming], is_pull=False, node_name='rep-1')

        client.refresh_from_db()
        self.assertEqual(client.name, 'Rep Old Edit')      # push still wins
        self.assertEqual(stats['conflicts'], 1)
        self.assertTrue(SyncConflict.objects.filter(
            reason='stale_write', sync_id=client.sync_id).exists())

    def test_fresh_push_is_not_flagged(self):
        client = Client.objects.create(name='Server Name')
        newer = (client.sync_updated_at + timedelta(minutes=5)).isoformat()
        incoming = row('panel.Client', client.sync_id, name='Rep New Edit')
        incoming['sync_updated_at'] = newer

        stats = apply_batch([incoming], is_pull=False, node_name='rep-1')

        client.refresh_from_db()
        self.assertEqual(client.name, 'Rep New Edit')
        self.assertEqual(stats['conflicts'], 0)
        self.assertFalse(SyncConflict.objects.filter(reason='stale_write').exists())

    def test_single_author_model_is_not_audited(self):
        # Expense is write-only for reps (not shared), so no collision audit
        # even when the incoming edit looks older than the server copy.
        expense = Expense.objects.create(description='Taxi', amount=Decimal('100'),
                                         date=date(2026, 8, 1))
        older = (expense.sync_updated_at - timedelta(minutes=5)).isoformat()
        incoming = row('panel.Expense', expense.sync_id, amount='200')
        incoming['sync_updated_at'] = older

        stats = apply_batch([incoming], is_pull=False, node_name='rep-1')

        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal('200'))
        self.assertEqual(stats['conflicts'], 0)
        self.assertFalse(SyncConflict.objects.filter(reason='stale_write').exists())

    def test_sale_is_shared_so_it_is_audited(self):
        # The manager and every salesperson hold and edit every sale.
        sale = Sale.objects.create(total=Decimal('1000'))
        older = (sale.sync_updated_at - timedelta(minutes=5)).isoformat()
        incoming = row('panel.Sale', sale.sync_id, total='2000')
        incoming['sync_updated_at'] = older

        stats = apply_batch([incoming], is_pull=False, node_name='rep-1')

        sale.refresh_from_db()
        self.assertEqual(sale.total, Decimal('2000'))      # push still wins
        self.assertEqual(stats['conflicts'], 1)


class ReferenceIdentityTests(SyncSetup):
    """A seed reference row (Currency) carries a different sync_id per database
    when nodes were seeded before ids were deterministic. Pulling the server's
    copy must reconcile it onto the local row by its unique natural key
    (``code``) instead of inserting a duplicate — which used to trip
    ``UNIQUE constraint failed: finance_currency.code`` and abort the whole pull.

    The three currencies already exist here: the 0006 seed migration runs during
    test-database setup, exactly as on a real laptop.
    """

    def test_currency_reconciled_by_code_not_duplicated(self):
        local = Currency.objects.get(code='USD')      # seeded locally
        server_sync_id = uuid.uuid4()                 # server's divergent id
        self.assertNotEqual(str(local.sync_id), str(server_sync_id))

        stats = apply_batch(
            [row('finance.Currency', server_sync_id, code='USD', name='US Dollar')],
            is_pull=True, node_name='mgr-1')

        self.assertEqual(Currency.objects.filter(code='USD').count(), 1)  # no dupe
        local.refresh_from_db()
        self.assertEqual(str(local.sync_id), str(server_sync_id))         # id adopted
        self.assertEqual(local.name, 'US Dollar')                         # content updated
        self.assertEqual(stats['applied'], 1)

    def test_currency_reconciled_even_when_content_matches(self):
        # Same name on both sides: only the identity differs. The sync_id must
        # still converge so future pulls (and FK references) resolve.
        local = Currency.objects.get(code='SDG')
        server_sync_id = uuid.uuid4()

        apply_batch(
            [row('finance.Currency', server_sync_id, code='SDG', name=local.name)],
            is_pull=True, node_name='mgr-1')

        self.assertEqual(Currency.objects.filter(code='SDG').count(), 1)
        local.refresh_from_db()
        self.assertEqual(str(local.sync_id), str(server_sync_id))

    def test_second_pull_is_a_clean_noop(self):
        server_sync_id = uuid.uuid4()
        name = Currency.objects.get(code='AED').name
        r = row('finance.Currency', server_sync_id, code='AED', name=name)

        apply_batch([r], is_pull=True, node_name='mgr-1')   # converges identity
        stats = apply_batch([r], is_pull=True, node_name='mgr-1')  # already matches

        self.assertEqual(Currency.objects.filter(code='AED').count(), 1)
        self.assertEqual(stats['noop'], 1)
        self.assertEqual(stats['applied'], 0)


class ApplySideEffectTests(SyncSetup):
    """A pushed row must persist without firing its model's side effects during
    apply. InvoicePayment.save() used to call invoice.update_status() mid-apply,
    whose stray print crashed the whole push on a server with a closed stdout
    (``OSError: [Errno 5]``). The status instead rides in on the Invoice row.
    """

    def _invoice(self):
        sale = Sale.objects.create(total=Decimal('2000'))
        SaleItem.objects.create(
            sale=sale, inventory=self.inventory, quantity=2, price=Decimal('1000'))
        return Invoice.objects.create(sale=sale, total=Decimal('2000'),
                                      status='unpaid')

    def test_pushed_payment_persists_without_recomputing_status(self):
        invoice = self._invoice()
        pay_id = uuid.uuid4()

        stats = apply_batch(
            [row('panel.InvoicePayment', pay_id, invoice=str(invoice.sync_id),
                 amount='500', paid_at='2026-08-07T10:00:00+00:00')],
            is_pull=False, node_name='rep-1', authoritative_inventory=True)

        self.assertEqual(stats['applied'], 1)
        self.assertTrue(InvoicePayment.objects.filter(sync_id=pay_id).exists())
        invoice.refresh_from_db()
        # update_status() was skipped during apply -> status untouched here.
        self.assertEqual(invoice.status, 'unpaid')

    def test_status_arrives_via_the_pushed_invoice_row(self):
        invoice = self._invoice()

        apply_batch(
            [row('panel.Invoice', invoice.sync_id, sale=str(invoice.sale.sync_id),
                 total='2000', status='partial', number=invoice.number),
             row('panel.InvoicePayment', uuid.uuid4(),
                 invoice=str(invoice.sync_id), amount='500',
                 paid_at='2026-08-07T10:00:00+00:00')],
            is_pull=False, node_name='rep-1', authoritative_inventory=True)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'partial')   # from the Invoice row

    def test_pulled_return_deletion_leaves_local_stock_untouched(self):
        # ReturnedProduct.delete() must skip its inventory/total side effects
        # during apply — the server recomputes stock and it's pulled down. On a
        # laptop pull there is no recompute, so touching stock here corrupts it.
        sale = Sale.objects.create(total=Decimal('2000'))
        item = SaleItem.objects.create(
            sale=sale, inventory=self.inventory, quantity=2, price=Decimal('1000'))
        rp_id = uuid.uuid4()
        apply_batch(
            [row('panel.ReturnedProduct', rp_id, sale=str(sale.sync_id),
                 sale_item=str(item.sync_id), quantity=1,
                 created_at='2026-08-07T10:00:00+00:00')],
            is_pull=True, node_name='mgr-1')
        self.inventory.refresh_from_db()
        qty_before = self.inventory.quantity

        apply_batch([row('panel.ReturnedProduct', rp_id, deleted=True)],
                    is_pull=True, node_name='mgr-1')

        self.assertFalse(ReturnedProduct.objects.filter(sync_id=rp_id).exists())
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, qty_before)  # guard held


class InvoiceNumberTests(SyncSetup):
    """Invoice numbers are prefixed with this node's digit so two laptops draw
    from disjoint ranges and never collide on the unique `number` when syncing."""

    @override_settings(SYNC_NODE_NUMBER=3)
    def test_number_uses_node_prefix(self):
        sale = Sale.objects.create(total=Decimal('1000'))
        invoice = Invoice.objects.create(sale=sale, total=Decimal('1000'))
        self.assertEqual(len(invoice.number), 7)
        self.assertTrue(invoice.number.startswith('3'))

    @override_settings(SYNC_NODE_NUMBER=7)
    def test_two_nodes_ranges_do_not_overlap(self):
        s1 = Sale.objects.create(total=Decimal('1'))
        n7 = Invoice.objects.create(sale=s1, total=Decimal('1')).number
        with override_settings(SYNC_NODE_NUMBER=2):
            s2 = Sale.objects.create(total=Decimal('1'))
            n2 = Invoice.objects.create(sale=s2, total=Decimal('1')).number
        self.assertTrue(n7.startswith('7') and n2.startswith('2'))
        self.assertNotEqual(n7, n2)


class CompositeIdentityTests(SyncSetup):
    """Reference rows created independently on two nodes share a natural key but
    not a sync_id. Apply must reconcile them, not collide on the unique key."""

    def test_commission_reconciled_by_employee_and_sale(self):
        sale = Sale.objects.create(employee=self.employee, total=Decimal('1000'))
        local = Commission.objects.create(
            employee=self.employee, sale=sale, amount=Decimal('50'))
        incoming_id = uuid.uuid4()
        self.assertNotEqual(str(local.sync_id), str(incoming_id))

        apply_batch(
            [row('panel.Commission', incoming_id,
                 employee=str(self.employee.sync_id), sale=str(sale.sync_id),
                 amount='75', paid_amount='0')],
            is_pull=False, node_name='mgr-2')

        self.assertEqual(
            Commission.objects.filter(employee=self.employee, sale=sale).count(), 1)
        local.refresh_from_db()
        self.assertEqual(str(local.sync_id), str(incoming_id))   # id adopted
        self.assertEqual(local.amount, Decimal('75'))

    def test_inventory_reconciled_by_shipment(self):
        incoming_id = uuid.uuid4()
        self.assertNotEqual(str(self.inventory.sync_id), str(incoming_id))

        apply_batch(
            [row('panel.Inventory', incoming_id, product=str(self.product.sync_id),
                 shipment=str(self.shipment.sync_id), quantity=9)],
            is_pull=True, node_name='mgr-1')

        self.assertEqual(
            Inventory.objects.filter(shipment=self.shipment).count(), 1)  # no dupe
        self.inventory.refresh_from_db()
        self.assertEqual(str(self.inventory.sync_id), str(incoming_id))
        self.assertEqual(self.inventory.quantity, 9)


class FullSalesSyncTests(SyncSetup):
    """Sales, invoices, payments and returns are shared by every laptop: what the
    manager adds or deletes reaches the salespeople on their next sync."""

    def _manager_sale(self):
        ids = [uuid.uuid4() for _ in range(4)]
        sale_uid, item_uid, inv_uid, pay_uid = ids
        client = Client.objects.create(name='Customer')
        self.push('tok-mgr', [
            row('panel.Sale', sale_uid, client=str(client.sync_id),
                employee=str(self.employee.sync_id),
                created_at='2026-09-01T10:00:00+00:00', total='4000'),
            row('panel.SaleItem', item_uid, sale=str(sale_uid),
                inventory=str(self.inventory.sync_id), quantity=2, price='2000'),
            row('panel.Invoice', inv_uid, sale=str(sale_uid), total='4000',
                status='partial', number='1000001',
                created_at='2026-09-01T10:00:00+00:00'),
            row('panel.InvoicePayment', pay_uid, invoice=str(inv_uid),
                amount='1500', paid_at='2026-09-02T10:00:00+00:00'),
        ])
        return ids

    def test_salesperson_receives_manager_sales(self):
        ids = self._manager_sale()
        data = self.pull('tok-sales').json()
        received = {r['sync_id'] for r in data['changes'] if not r['is_deleted']}
        for uid in ids:
            self.assertIn(str(uid), received)

    def test_manager_deletion_reaches_salesperson(self):
        sale_uid = self._manager_sale()[0]
        cursor = self.pull('tok-sales').json()['cursor']

        self.push('tok-mgr', [row('panel.Sale', sale_uid, deleted=True)])

        data = self.pull('tok-sales', cursor).json()
        self.assertIn(('panel.Sale', str(sale_uid)),
                      [(r['label'], r['sync_id']) for r in data['changes']
                       if r['is_deleted']])

    def test_pulled_sale_deletion_removes_the_whole_invoice(self):
        # Laptop side: the Sale tombstone takes its items, invoice and payments.
        sale = Sale.objects.create(total=Decimal('2000'))
        item = SaleItem.objects.create(
            sale=sale, inventory=self.inventory, quantity=2, price=Decimal('1000'))
        invoice = Invoice.objects.create(sale=sale, total=Decimal('2000'))
        payment = InvoicePayment.objects.create(invoice=invoice, amount=Decimal('500'))
        self.inventory.refresh_from_db()
        qty_before = self.inventory.quantity

        apply_batch([row('panel.Sale', sale.sync_id, deleted=True)],
                    is_pull=True, node_name='mgr-1')

        self.assertFalse(Sale.objects.filter(pk=sale.pk).exists())
        self.assertFalse(SaleItem.objects.filter(pk=item.pk).exists())
        self.assertFalse(Invoice.objects.filter(pk=invoice.pk).exists())
        self.assertFalse(InvoicePayment.objects.filter(pk=payment.pk).exists())
        self.inventory.refresh_from_db()
        # Stock comes down as the server's recomputed Inventory row instead.
        self.assertEqual(self.inventory.quantity, qty_before)

    def test_deletions_reach_salesperson_for_models_it_cannot_read(self):
        # A rep's own expense deleted by the manager must leave the rep's laptop
        # too, although expenses are never sent down.
        exp_uid = uuid.uuid4()
        self.push('tok-sales', [row('panel.Expense', exp_uid, description='Taxi',
                                    amount='500', date='2026-08-10')])
        cursor = self.pull('tok-sales').json()['cursor']

        self.push('tok-mgr', [row('panel.Expense', exp_uid, deleted=True)])

        data = self.pull('tok-sales', cursor).json()
        self.assertEqual([(r['label'], r['is_deleted']) for r in data['changes']
                          if r['label'] == 'panel.Expense'],
                         [('panel.Expense', True)])


class DeleteWinsTests(SyncSetup):
    """A laptop that re-saves its old copy of a deleted sale (an edit, a return
    recalculating the total) must not bring the sale back for everyone."""

    def setUp(self):
        super().setUp()
        self.sale_uid = uuid.uuid4()
        self.sale_row = row('panel.Sale', self.sale_uid,
                            employee=str(self.employee.sync_id),
                            created_at='2026-09-01T10:00:00+00:00', total='4000')
        self.push('tok-sales', [self.sale_row])
        self.push('tok-mgr', [row('panel.Sale', self.sale_uid, deleted=True)])

    def test_stale_copy_is_not_restored(self):
        data = self.push('tok-sales', [self.sale_row]).json()

        self.assertTrue(data['ok'])
        self.assertEqual(data['applied'], 0)
        self.assertEqual(data['conflicts'], 1)
        self.assertFalse(Sale.objects.filter(sync_id=self.sale_uid).exists())
        self.assertTrue(SyncConflict.objects.filter(
            sync_id=self.sale_uid, reason='stale_write').exists())

    def test_refused_copy_gets_the_deletion_again(self):
        # That laptop's cursor is already past the original deletion.
        cursor = self.pull('tok-sales').json()['cursor']

        self.push('tok-sales', [self.sale_row])

        data = self.pull('tok-sales', cursor).json()
        self.assertIn(('panel.Sale', str(self.sale_uid)),
                      [(r['label'], r['sync_id']) for r in data['changes']
                       if r['is_deleted']])


class PullPolicyTests(SyncSetup):
    """A laptop whose cursor was earned under an older policy (before sales were
    sent to salespeople) gets everything resent once."""

    def test_salesperson_on_old_policy_gets_everything_again(self):
        cursor = self.pull('tok-sales').json()['cursor']

        data = self.pull('tok-sales', cursor, policy='').json()

        self.assertTrue(data['changes'])
        self.assertEqual(data['policy'], registry.pull_policy('salesperson'))

    def test_current_policy_stays_incremental(self):
        first = self.pull('tok-sales', policy='').json()

        data = self.pull('tok-sales', first['cursor'], policy=first['policy']).json()

        self.assertEqual(data['changes'], [])

    def test_laptop_without_policy_stays_incremental(self):
        cursor = self.pull('tok-sales').json()['cursor']
        self.assertEqual(self.pull('tok-sales', cursor).json()['changes'], [])

    def test_manager_is_never_forced_to_resend(self):
        cursor = self.pull('tok-mgr').json()['cursor']

        data = self.pull('tok-mgr', cursor, policy='').json()

        self.assertEqual(data['changes'], [])
        self.assertEqual(data['policy'], '')

    def test_version_bump_changes_the_fingerprint(self):
        before = registry.pull_policy('salesperson')
        with mock.patch.object(registry, 'PULL_POLICY_VERSION',
                               registry.PULL_POLICY_VERSION + 1):
            self.assertNotEqual(registry.pull_policy('salesperson'), before)


class ClientPullPolicyTests(SyncSetup):
    """Laptop side of the policy handshake (sync.client.pull)."""

    def _serve(self, *responses):
        sent = []

        def fake_post(path, payload):
            sent.append(payload)
            return responses[len(sent) - 1]
        return sent, mock.patch('sync.client._post', side_effect=fake_post)

    def test_reports_and_stores_the_policy(self):
        sent, patched = self._serve(
            {'ok': True, 'changes': [], 'cursor': None, 'policy': 'abc'},
            {'ok': True, 'changes': [], 'cursor': None, 'policy': 'abc'})
        with patched:
            client_pull()
            client_pull()
        self.assertEqual([p['policy'] for p in sent], ['', 'abc'])
        self.assertEqual(SyncState.get().pull_policy, 'abc')

    def test_old_server_leaves_the_policy_alone(self):
        _, patched = self._serve({'ok': True, 'changes': [], 'cursor': None})
        with patched:
            client_pull()
        self.assertEqual(SyncState.get().pull_policy, '')

    def test_failed_apply_stores_neither_policy_nor_cursor(self):
        bad = row('panel.Sale', uuid.uuid4(), total='not-a-number')
        _, patched = self._serve({'ok': True, 'changes': [bad],
                                  'cursor': '2026-09-01T10:00:00+00:00',
                                  'policy': 'abc'})
        with patched, self.assertRaises(Exception):
            client_pull()
        state = SyncState.get()
        self.assertEqual(state.pull_policy, '')
        self.assertIsNone(state.last_pull_cursor)


class PushedCommissionTests(SyncSetup):
    """The server gives pushed sales their commissions. A salesperson laptop
    holds no commission percentages, so its sales arrive without one."""

    def setUp(self):
        super().setUp()
        self.employee.commission_percentage = Decimal('10')
        self.employee.save()

    def _push_sale(self, sale_uid, total, token='tok-sales'):
        return self.push(token, [
            row('panel.Sale', sale_uid, employee=str(self.employee.sync_id),
                created_at='2026-09-01T10:00:00+00:00', total=total),
        ])

    def _commission(self, sale_uid):
        return Commission.objects.get(employee=self.employee, sale__sync_id=sale_uid)

    def test_salesperson_sale_gets_commission(self):
        sale_uid = uuid.uuid4()
        self.assertEqual(self._push_sale(sale_uid, '5000000').status_code, 200)
        commission = self._commission(sale_uid)
        self.assertEqual(commission.amount, Decimal('500000'))
        self.assertEqual(commission.created_at, parse_datetime('2026-09-01T10:00:00+00:00'))
        # It reaches the manager laptops; salespeople still never see commissions.
        def labels(token):
            return {r['label'] for r in self.pull(token).json()['changes']}
        self.assertIn('panel.Commission', labels('tok-mgr'))
        self.assertNotIn('panel.Commission', labels('tok-sales'))

    def test_pushed_total_change_updates_commission(self):
        sale_uid = uuid.uuid4()
        self._push_sale(sale_uid, '1000')
        self._push_sale(sale_uid, '800')  # e.g. a return recorded on the laptop
        self.assertEqual(self._commission(sale_uid).amount, Decimal('80'))

    def test_pushed_percentage_reaches_sales_the_laptop_had_not_seen(self):
        sale_uid = uuid.uuid4()
        self._push_sale(sale_uid, '1000')
        self.push('tok-mgr', [row('panel.Employee', self.employee.sync_id,
                                  commission_percentage='20')])
        self.assertEqual(self._commission(sale_uid).amount, Decimal('200'))

    def test_partly_paid_commission_not_rerated_twice(self):
        sale_uid = uuid.uuid4()
        self._push_sale(sale_uid, '1000')
        commission = self._commission(sale_uid)
        commission.paid_amount = Decimal('50')
        commission.save()
        # The manager laptop re-rated the unpaid 50 from 10% to 20% (-> 150)
        # and pushes the result with the percentage change.
        self.push('tok-mgr', [
            row('panel.Employee', self.employee.sync_id, commission_percentage='20'),
            row('panel.Commission', commission.sync_id,
                employee=str(self.employee.sync_id), sale=str(sale_uid),
                amount='150', paid_amount='50'),
        ])
        self.assertEqual(self._commission(sale_uid).amount, Decimal('150'))

    def test_pushed_reassignment_moves_commission(self):
        sale_uid = uuid.uuid4()
        self._push_sale(sale_uid, '1000')
        other = Employee.objects.create(name='Rep Two', commission_percentage=Decimal('5'))
        self.push('tok-sales', [row('panel.Sale', sale_uid, employee=str(other.sync_id))])
        self.assertEqual(self._commission(sale_uid).amount, Decimal('0'))
        self.assertEqual(Commission.objects.get(employee=other, sale__sync_id=sale_uid).amount,
                         Decimal('50'))

    def test_pull_leaves_commissions_to_the_server(self):
        apply_batch([row('panel.Sale', uuid.uuid4(), employee=str(self.employee.sync_id),
                         total='1000')], is_pull=True, node_name='mgr-1')
        self.assertFalse(Commission.objects.exists())
