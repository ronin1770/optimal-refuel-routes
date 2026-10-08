from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("stations", "0002_station_rtree")]
    operations = [
        migrations.AddField(
            model_name="station",
            name="geocoding_decision_summary",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="geocodingattempt",
            name="candidate_decisions",
            field=models.JSONField(default=list),
        ),
    ]
