from django.urls import path

from . import views

urlpatterns = [
    path("sign-in/", views.sign_in, name="sign_in"),
    path("auth/session/", views.auth_session, name="auth_session"),
    path("sign-out/", views.sign_out, name="sign_out"),
    path("health/", views.health, name="health"),
    path("", views.dashboard, name="dashboard"),
    path("products/", views.product_list, name="products"),
    path("products/new/", views.product_create, name="product_create"),
    path("products/<int:product_id>/edit/", views.product_edit, name="product_edit"),
    path("products/<int:product_id>/delete/", views.product_delete, name="product_delete"),
    path("products/import/", views.product_import, name="product_import"),
    path("products/import/template.csv", views.product_import_template, name="product_import_template"),
    path("stock/receive/", views.receive_stock, name="receive_stock"),
    path("stock/adjustments/", views.stock_adjustments, name="stock_adjustments"),
    path("sell/", views.point_of_sale, name="pos"),
    path("sales/", views.sales, name="sales"),
    path("sales/<uuid:sale_id>/void/", views.void_sale, name="void_sale"),
    path("reports/", views.reports, name="reports"),
    path("reports/export.csv", views.reports_csv, name="reports_csv"),
    path("returns/", views.returns, name="returns"),
    path("returns/<uuid:return_id>/", views.return_receipt, name="return_receipt"),
    path("team/", views.team, name="team"),
    path("settings/", views.shop_settings, name="shop_settings"),
    path("suppliers/", views.suppliers, name="suppliers"),
    path("expenses/", views.expenses, name="expenses"),
    path("receipts/<uuid:sale_id>/", views.receipt, name="receipt"),
]
