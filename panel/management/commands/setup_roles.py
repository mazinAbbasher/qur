"""Create the manager/salesperson groups and, optionally, users.

Examples
--------
Create just the groups (idempotent, safe to re-run)::

    python manage.py setup_roles

Create a salesperson login::

    python manage.py setup_roles --salesperson ahmed --password "s3cret"

Create a manager login::

    python manage.py setup_roles --manager sara --password "s3cret"
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from panel.permissions import MANAGER_GROUP, SALESPERSON_GROUP


class Command(BaseCommand):
    help = "Create manager/salesperson groups and optionally create users."

    def add_arguments(self, parser):
        parser.add_argument('--salesperson', metavar='USERNAME',
                            help='Create (or update) a salesperson user with this username.')
        parser.add_argument('--manager', metavar='USERNAME',
                            help='Create (or update) a manager user with this username.')
        parser.add_argument('--password', metavar='PASSWORD',
                            help='Password for the created user (required with --salesperson/--manager).')

    def handle(self, *args, **options):
        manager_group, _ = Group.objects.get_or_create(name=MANAGER_GROUP)
        sales_group, _ = Group.objects.get_or_create(name=SALESPERSON_GROUP)
        self.stdout.write(self.style.SUCCESS(
            f"Groups ready: '{MANAGER_GROUP}', '{SALESPERSON_GROUP}'."))

        User = get_user_model()

        def upsert(username, group, is_staff=False):
            password = options.get('password')
            if not password:
                raise CommandError("--password is required when creating a user.")
            user, created = User.objects.get_or_create(username=username)
            user.set_password(password)
            user.is_active = True
            user.is_staff = is_staff
            user.save()
            # Make role exclusive so a manager isn't also in the salesperson group.
            user.groups.remove(manager_group, sales_group)
            user.groups.add(group)
            verb = 'Created' if created else 'Updated'
            self.stdout.write(self.style.SUCCESS(
                f"{verb} {group.name} user '{username}'."))

        if options.get('salesperson'):
            upsert(options['salesperson'], sales_group, is_staff=False)
        if options.get('manager'):
            upsert(options['manager'], manager_group, is_staff=True)
