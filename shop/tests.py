import json
import uuid
from decimal import Decimal

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from .decorators import permission_required
from .forms import ProductForm
from .models import Membership, Payment, Product, Sale, SaleLine, Shop, StockMovement, Supplier
from .services.reporting import build_daily_financials, build_report, calculate_financials
from .views import health, point_of_sale
from django.utils import timezone


class ProductSupplierScopeTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Shop One")
        self.other_shop = Shop.objects.create(name="Shop Two")
        self.supplier = Supplier.objects.create(shop=self.shop, name="Local Supplier")
        self.other_supplier = Supplier.objects.create(shop=self.other_shop, name="Outside Supplier")

    def test_product_supplier_choices_are_limited_to_the_current_shop(self):
        form = ProductForm(shop=self.shop)
        self.assertQuerySetEqual(
            form.fields["supplier"].queryset.order_by("pk"),
            [self.supplier],
        )
        self.assertNotIn(str(self.other_supplier.pk), str(form["supplier"]))

    def test_supplier_date_uses_only_its_shops_creation_audit(self):
        from datetime import datetime, timezone as dt_timezone
        from .models import AuditEvent
        from .views import suppliers

        for shop, event_type, year in (
            (self.shop, "supplier.created", 2024),
            (self.other_shop, "supplier.created", 2023),
            (self.shop, "supplier.archived", 2022),
        ):
            AuditEvent.objects.create(
                shop=shop, event_type=event_type, target_type="supplier",
                target_id=str(self.supplier.pk),
                created_at=datetime(year, 6, 15, 12, tzinfo=dt_timezone.utc),
            )
        request = RequestFactory().get("/suppliers/")
        request.membership = Membership(shop=self.shop, role=Membership.Role.ADMIN)
        response = suppliers(request)
        self.assertContains(response, "15 Jun 2024")
        self.assertNotContains(response, "15 Jun 2023")
        self.assertNotContains(response, "15 Jun 2022")
        self.assertNotContains(response, self.other_supplier.name)

    def test_supplier_without_creation_audit_has_no_invented_date(self):
        from .views import suppliers

        request = RequestFactory().get("/suppliers/")
        request.membership = Membership(shop=self.shop, role=Membership.Role.ADMIN)
        response = suppliers(request)
        self.assertContains(response, "Date added</span><strong>Not recorded</strong>")


class PermissionDecoratorTests(SimpleTestCase):
    def test_permission_is_enforced_server_side(self):
        @permission_required("refunds")
        def protected_view(request):
            from django.http import HttpResponse
            return HttpResponse("allowed")

        staff = type("Staff", (), {"can": lambda self, capability: False})()
        request = type("Request", (), {"membership": staff})()
        self.assertEqual(protected_view(request).status_code, 403)

        admin = type("Admin", (), {"can": lambda self, capability: True})()
        request.membership = admin
        self.assertEqual(protected_view(request).status_code, 200)


class HealthCheckTests(TestCase):
    def test_health_check_requires_database_and_cache(self):
        response = health(RequestFactory().get("/health/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")


class CheckoutIdempotencyTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Shop One")
        self.membership = Membership.objects.create(
            shop=self.shop,
            supabase_user_id="user_test_admin",
            role=Membership.Role.ADMIN,
        )
        self.product = Product.objects.create(
            shop=self.shop,
            name="USB cable",
            kind=Product.Kind.STOCK,
            tracking=Product.Tracking.QUANTITY,
            price=Decimal("100.00"),
            cost=Decimal("40.00"),
        )
        StockMovement.objects.create(
            shop=self.shop,
            product=self.product,
            quantity_delta=5,
            reason=StockMovement.Reason.OPENING,
            actor=self.membership,
        )

    def _post_checkout(self, request_key, **customer_details):
        request = RequestFactory().post("/sell/", {
            "request_key": str(request_key),
            "cart_json": json.dumps([{"product_id": self.product.pk, "quantity": 1}]),
            "tender": "cash",
            **customer_details,
        })
        request.membership = self.membership
        return point_of_sale(request)

    def test_retry_with_same_key_creates_only_one_sale_payment_and_stock_movement(self):
        request_key = uuid.uuid4()
        first_response = self._post_checkout(request_key)
        second_response = self._post_checkout(request_key)

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(Sale.objects.filter(shop=self.shop).count(), 1)
        sale = Sale.objects.get(shop=self.shop)
        self.assertEqual(Payment.objects.filter(sale=sale).count(), 1)
        self.assertEqual(StockMovement.objects.filter(sale=sale).count(), 1)
        self.assertEqual(self.product.on_hand, 4)

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_invoice_preserves_contact_details_after_shop_settings_change(self):
        from .views import receipt

        self.shop.address = "Original shop address"
        self.shop.phone = "9000000000"
        self.shop.email = "original@example.com"
        self.shop.save()
        self._post_checkout(uuid.uuid4(), customer_phone=" 9111111111 ")
        sale = Sale.objects.get(shop=self.shop)

        self.shop.address = "Updated shop address"
        self.shop.phone = "9222222222"
        self.shop.email = "updated@example.com"
        self.shop.save()
        request = RequestFactory().get(f"/receipts/{sale.pk}/")
        request.membership = self.membership
        response = receipt(request, sale.pk)

        self.assertEqual(sale.customer_phone, "9111111111")
        for original in ("Original shop address", "9000000000", "original@example.com", "9111111111"):
            self.assertContains(response, original)
        for updated in ("Updated shop address", "9222222222", "updated@example.com"):
            self.assertNotContains(response, updated)

    def test_service_without_cost_does_not_mark_profit_incomplete(self):
        service = Product.objects.create(
            shop=self.shop,
            name="Phone setup service",
            kind=Product.Kind.SERVICE,
            price=Decimal("250.00"),
            cost=None,
        )
        sale = Sale.objects.create(
            shop=self.shop,
            receipt_number="test-service-1",
            cashier=self.membership,
            shop_name_snapshot=self.shop.name,
            currency=self.shop.currency,
            subtotal=Decimal("250.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("250.00"),
        )
        SaleLine.objects.create(
            sale=sale,
            product=service,
            description=service.name,
            item_kind=Product.Kind.SERVICE,
            quantity=1,
            unit_price=Decimal("250.00"),
            unit_cost=None,
            tax_amount=Decimal("0.00"),
            line_total=Decimal("250.00"),
        )
        result = calculate_financials(self.shop, timezone.localdate(), timezone.localdate())
        self.assertEqual(result["missing_cost_units"], 0)
        self.assertEqual(result["profit"], Decimal("250.00"))

    def test_daily_dashboard_aggregation_matches_single_day_financials(self):
        sale = Sale.objects.create(
            shop=self.shop,
            receipt_number="test-daily-chart-1",
            cashier=self.membership,
            shop_name_snapshot=self.shop.name,
            currency=self.shop.currency,
            subtotal=Decimal("100.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("100.00"),
        )
        SaleLine.objects.create(
            sale=sale,
            product=self.product,
            description=self.product.name,
            item_kind=Product.Kind.STOCK,
            quantity=1,
            unit_price=Decimal("100.00"),
            unit_cost=Decimal("40.00"),
            tax_amount=Decimal("0.00"),
            line_total=Decimal("100.00"),
        )
        today = timezone.localdate()
        visible_sales = Sale.objects.filter(shop=self.shop)
        optimized = build_daily_financials(self.shop, today, today, sales=visible_sales, sale_scope=visible_sales)
        reference = calculate_financials(self.shop, today, today)
        self.assertEqual(optimized[today]["net_sales"], reference["net_sales"])
        self.assertEqual(optimized[today]["profit"], reference["profit"])

    def test_report_item_aggregation_preserves_line_totals(self):
        sale = Sale.objects.create(
            shop=self.shop,
            receipt_number="test-aggregated-report-1",
            cashier=self.membership,
            shop_name_snapshot=self.shop.name,
            currency=self.shop.currency,
            subtotal=Decimal("200.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("200.00"),
        )
        SaleLine.objects.create(
            sale=sale,
            product=self.product,
            description=self.product.name,
            item_kind=Product.Kind.STOCK,
            quantity=2,
            unit_price=Decimal("100.00"),
            unit_cost=Decimal("40.00"),
            tax_amount=Decimal("0.00"),
            line_total=Decimal("200.00"),
        )
        from .models import ReturnLine, SaleReturn
        sale_line = sale.lines.get()
        return_record = SaleReturn.objects.create(
            sale=sale,
            created_by=self.membership,
            reason="Customer changed mind",
            refund_total=Decimal("50.00"),
            refund_tender="cash",
        )
        ReturnLine.objects.create(
            return_record=return_record,
            sale_line=sale_line,
            quantity=1,
            disposition=ReturnLine.Disposition.SELLABLE,
            refund_amount=Decimal("50.00"),
            tax_refund_amount=Decimal("0.00"),
        )
        today = timezone.localdate()
        report = build_report(self.shop, today, today)
        self.assertEqual(report["net_sales"], Decimal("150.00"))
        self.assertEqual(report["products"][0]["units"], 1)
        self.assertEqual(report["products"][0]["revenue"], Decimal("150.00"))
        self.assertEqual(report["products"][0]["cost"], Decimal("40.00"))


class InventoryAndReturnIntegrityTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Shop One")
        self.membership = Membership.objects.create(
            shop=self.shop,
            supabase_user_id="user_inventory_admin",
            role=Membership.Role.ADMIN,
        )

    def test_imei_is_unique_within_a_shop(self):
        from django.db import IntegrityError, transaction
        from .models import Device

        product = Product.objects.create(
            shop=self.shop,
            name="Serialized phone",
            tracking=Product.Tracking.SERIALIZED,
        )
        Device.objects.create(shop=self.shop, product=product, imei="123456789012345")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Device.objects.create(shop=self.shop, product=product, imei="123456789012345")

    def test_sellable_return_links_refund_and_restores_one_piece(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from .models import ReturnLine, SaleReturn
        from .views import returns

        product = Product.objects.create(
            shop=self.shop,
            name="Phone case",
            kind=Product.Kind.STOCK,
            tracking=Product.Tracking.QUANTITY,
            price=Decimal("100.00"),
            cost=Decimal("40.00"),
        )
        sale = Sale.objects.create(
            shop=self.shop,
            receipt_number="return-test-1",
            cashier=self.membership,
            shop_name_snapshot=self.shop.name,
            currency=self.shop.currency,
            subtotal=Decimal("200.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("200.00"),
        )
        line = SaleLine.objects.create(
            sale=sale,
            product=product,
            description=product.name,
            item_kind=Product.Kind.STOCK,
            quantity=2,
            unit_price=Decimal("100.00"),
            unit_cost=Decimal("40.00"),
            tax_amount=Decimal("0.00"),
            line_total=Decimal("200.00"),
        )
        StockMovement.objects.create(shop=self.shop, product=product, quantity_delta=2, reason=StockMovement.Reason.OPENING, actor=self.membership)
        StockMovement.objects.create(shop=self.shop, product=product, quantity_delta=-2, reason=StockMovement.Reason.SALE, sale=sale, actor=self.membership)
        return_key = uuid.uuid4()
        request = RequestFactory().post("/returns/", {
            "request_key": str(return_key),
            "receipt_number": sale.receipt_number,
            "line_id": str(line.pk),
            "quantity": "1",
            "reason": "Customer changed mind",
            "refund_tender": "cash",
            "disposition": ReturnLine.Disposition.SELLABLE,
        })
        request.membership = self.membership
        request.session = {}
        request._messages = FallbackStorage(request)
        response = returns(request)

        self.assertEqual(response.status_code, 302)
        return_record = SaleReturn.objects.get(pk=return_key)
        self.assertEqual(return_record.refund_total, Decimal("100.00"))
        self.assertEqual(return_record.lines.get().disposition, ReturnLine.Disposition.SELLABLE)
        self.assertEqual(Payment.objects.filter(return_record=return_record, kind=Payment.Kind.REFUND).count(), 1)
        self.assertEqual(StockMovement.objects.filter(sale=sale, reason=StockMovement.Reason.RETURN).get().quantity_delta, 1)
        self.assertEqual(product.on_hand, 1)

        retry = RequestFactory().post("/returns/", {
            "request_key": str(return_key),
            "receipt_number": sale.receipt_number,
            "line_id": str(line.pk),
            "quantity": "1",
            "reason": "Customer changed mind",
            "refund_tender": "cash",
            "disposition": ReturnLine.Disposition.SELLABLE,
        })
        retry.membership = self.membership
        retry.session = {}
        retry._messages = FallbackStorage(retry)
        self.assertEqual(returns(retry).status_code, 302)
        self.assertEqual(SaleReturn.objects.filter(sale=sale).count(), 1)
        self.assertEqual(Payment.objects.filter(sale=sale, kind=Payment.Kind.REFUND).count(), 1)
        self.assertEqual(StockMovement.objects.filter(sale=sale, reason=StockMovement.Reason.RETURN).count(), 1)
