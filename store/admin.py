from django.contrib import admin

from .models import BosItem, Customer, Labour, Line, Stock

admin.site.register(Stock)
admin.site.register(BosItem)
admin.site.register(Customer)
admin.site.register(Line)
admin.site.register(Labour)
