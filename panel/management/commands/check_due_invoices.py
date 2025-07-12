from django.core.management.base import BaseCommand
from django.utils import timezone
from panel.models import Invoice
from datetime import timedelta

class Command(BaseCommand):
    help = 'Check for invoices due in 3 days and send alerts'

    def handle(self, *args, **kwargs):
        target_date = timezone.now().date() + timedelta(days=3)
        invoices = Invoice.objects.filter(due_date=target_date, status='unpaid')
        for invoice in invoices:
            # Replace this with actual email or system alert logic
            self.stdout.write(
                f"ALERT: Invoice #{invoice.pk} for client '{invoice.sale.client}' is due on {invoice.due_date}."
            )
