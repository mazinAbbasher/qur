"""Sync engine + API tests — the reliability guarantees, proven.

Covers: token auth, role-based push rejection, field stripping on pull,
idempotent upserts (no duplicates), FK-by-sync_id resolution regardless of
order, oversell protection, tombstones, and no-clobber-on-pull.
"""

import json
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from panel.models import (
    Client, Employee, Inventory, Product, Sale, SaleItem, Shipment,
)
from sync.engine import apply_batch
from sync.models import Node, SyncConflict, SyncOutbox
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

    def pull(self, token, since=None):
        return self.client.post(
            reverse('sync:api_pull'),
            data=json.dumps({'since': since}),
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
