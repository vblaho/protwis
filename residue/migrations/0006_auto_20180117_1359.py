# Generated by Django 2.0.1 on 2018-01-17 12:59

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('residue', '0005_auto_20171215_1457'),
    ]

    operations = [
        migrations.AlterField(
            model_name='residuedatapoint',
            name='residue',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='annotations', to='residue.Residue'),
        ),
    ]