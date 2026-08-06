"""Register a laptop on the SERVER and print its API token.

Run on the central server:

    python manage.py create_node "Ahmed-Laptop" --role salesperson
    python manage.py create_node "Manager-Laptop" --role manager

Copy the printed token into that laptop's .env as SYNC_NODE_TOKEN.
Use --rotate to issue a fresh token for an existing node.
"""

from django.core.management.base import BaseCommand, CommandError

from sync.models import Node


class Command(BaseCommand):
    help = "Register a sync node (laptop) and print its API token."

    def add_arguments(self, parser):
        parser.add_argument('name')
        parser.add_argument('--role', choices=['manager', 'salesperson'], required=True)
        parser.add_argument('--rotate', action='store_true',
                            help='Issue a new token for an existing node.')

    def handle(self, *args, **options):
        name = options['name']
        role = options['role']
        node, created = Node.objects.get_or_create(
            name=name, defaults={'role': role, 'token': Node.new_token()},
        )
        if not created:
            node.role = role
            if options['rotate']:
                node.token = Node.new_token()
            node.is_active = True
            node.save()

        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Updated'} node '{name}' (role={role})."))
        self.stdout.write("")
        self.stdout.write("  SYNC_NODE_TOKEN=" + node.token)
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "Store this token in the laptop's .env. It is shown in full here only."))
