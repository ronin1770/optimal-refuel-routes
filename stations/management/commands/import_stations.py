from django.core.management.base import BaseCommand, CommandError

from stations.services.csv_import import import_stations


class Command(BaseCommand):
    help = "Import canonical stations and source provenance from an OPIS CSV"

    def add_arguments(self, parser):
        parser.add_argument("csv_path")

    def handle(self, *args, **options):
        def report_error(line, message):
            self.stderr.write(f"line {line}: {message}")

        try:
            summary = import_stations(options["csv_path"], error_callback=report_error)
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            "imported={0.imported} unchanged={0.unchanged} invalid={0.invalid} "
            "conflicting={0.conflicting}".format(summary)
        )
