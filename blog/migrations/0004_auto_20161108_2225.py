# -*- coding: utf-8 -*-
# Generated by Django 1.9.11 on 2016-11-08 22:25
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0003_auto_20151212_0001'),
    ]

    operations = [
        migrations.AlterField(
            model_name='blogpost',
            name='date',
            field=models.DateTimeField(blank=True, editable=False, null=True, verbose_name=b'posted'),
        ),
    ]