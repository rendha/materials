from django.contrib.auth import views as auth_views
from django.urls import path

from . import pwa, views

urlpatterns = [
    path("manifest.webmanifest", pwa.manifest, name="manifest"),
    path("sw.js", pwa.service_worker, name="service_worker"),
    path("offline/", pwa.offline, name="offline"),
    path("login/", auth_views.LoginView.as_view(template_name="store/login.html", redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.home, name="home"),
    path("import/", views.import_quotation, name="import"),
    path("customer/<int:pk>/", views.customer_detail, name="customer"),
    path("customer/<int:pk>/export/<str:source>/", views.export_csv, name="export"),
    path("stock/", views.stock_list, name="stock"),
    path("boss/", views.boss_home, name="boss"),
    path("boss/<str:kind>/", views.boss_detail, name="boss_detail"),
    path("accounts/", views.accounts_home, name="accounts"),
    path("accounts/<int:pk>/", views.accounts_customer, name="accounts_customer"),
]
