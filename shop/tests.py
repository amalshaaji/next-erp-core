import json
import uuid
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from .decorators import permission_required
from .forms import ProductForm
from .models import Membership, Payment, Product, Sale, SaleLine, Shop, StockMovement, Supplier
from .services.reporting import build_daily_financials, build_report, calculate_financials
from .views import health, point_of_sale
from django.utils import timezone


@override_settings(SUPABASE_URL="https://example.supabase.co", SUPABASE_SERVICE_ROLE_KEY="private-test-key")
class SupabaseAccountErrorTests(SimpleTestCase):
    def reject_account(self, status, body):
        from .services.supabase_api import SupabaseApiError, create_user

        error = HTTPError("https://example.supabase.co/auth/v1/admin/users", status, "Rejected", {}, BytesIO(body))
        with patch("shop.services.supabase_api.urlopen", side_effect=error):
            with self.assertLogs("shop.services.supabase_api", level="WARNING") as logs:
                with self.assertRaises(SupabaseApiError) as caught:
                    create_user("member@example.com", "private-test-password")
        return str(caught.exception), " ".join(logs.output)

    def test_known_codes_explain_the_specific_failure(self):
        for code, expected in (
            ("email_exists", "already has a Supabase account"),
            ("user_already_exists", "already has a Supabase account"),
            ("weak_password", "rejected this password"),
            ("email_address_invalid", "rejected this email"),
            ("not_admin", "Admin key"),
            ("over_request_rate_limit", "Wait a few minutes"),
            ("unexpected_failure", "Auth logs"),
        ):
            with self.subTest(code=code):
                message, logs = self.reject_account(422, json.dumps({"error_code": code}).encode())
                self.assertIn(expected, message)
                self.assertIn(code, logs)

    def test_code_property_is_supported(self):
        message, _ = self.reject_account(422, b'{"code":"weak_password"}')
        self.assertIn("rejected this password", message)

    def test_unknown_or_malformed_errors_have_safe_status_messages(self):
        for status, expected in (
            (401, "Admin key"), (403, "Admin key"), (429, "Wait a few minutes"),
            (500, "Auth logs"), (422, "HTTP 422"),
        ):
            for body in (b'not JSON', b'[]', b'{"error_code":["bad"]}', b'{"code":422}'):
                with self.subTest(status=status, body=body):
                    message, _ = self.reject_account(status, body)
                    self.assertIn(expected, message)

    def test_errors_and_logs_never_expose_private_provider_data(self):
        for code in ("weak_password", "private-test-password member@example.com"):
            body = json.dumps({
                "error_code": code,
                "msg": "private-test-password member@example.com private-test-key",
            }).encode()
            message, logs = self.reject_account(422, body)
            for private_value in ("private-test-password", "member@example.com", "private-test-key"):
                self.assertNotIn(private_value, message + logs)


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


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ProductManagementTests(TestCase):
    def setUp(self):
        from django.urls import reverse

        self.shop = Shop.objects.create(name="Product management shop")
        self.admin = Membership.objects.create(shop=self.shop, role=Membership.Role.ADMIN)
        self.staff = Membership.objects.create(shop=self.shop, role=Membership.Role.STAFF)
        self.member = self.admin
        self.product = Product.objects.create(
            shop=self.shop, name="USB cable", sku="CABLE-1", price=Decimal("100.00"),
            cost=Decimal("40.00"), reorder_level=2,
        )
        StockMovement.objects.create(
            shop=self.shop, product=self.product, quantity_delta=5,
            reason=StockMovement.Reason.OPENING, actor=self.admin,
        )
        self.edit_url = reverse("product_edit", args=[self.product.pk])
        self.delete_url = reverse("product_delete", args=[self.product.pk])
        self.client.cookies["supabase_access_token"] = "test-session"
        for target, kwargs in (
            ("shop.middleware.verify_access_token", {"return_value": {"sub": "test-admin"}}),
            ("shop.middleware.resolve_membership", {"side_effect": lambda *args: self.member}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def data(self, **changes):
        return {
            "name": "Updated cable", "sku": "CABLE-1", "category": "Accessories",
            "brand_model": "USB-C", "supplier": "", "kind": "stock",
            "price": "150.00", "cost": "50.00", "reorder_level": "3", **changes,
        }

    def make_sale(self):
        sale = Sale.objects.create(
            shop=self.shop, cashier=self.admin, receipt_number="PRODUCT-1",
            shop_name_snapshot=self.shop.name, subtotal=Decimal("100.00"),
            tax_total=Decimal("0.00"), total=Decimal("100.00"),
        )
        SaleLine.objects.create(
            sale=sale, product=self.product, description=self.product.name,
            unit_price=self.product.price, unit_cost=self.product.cost, line_total=sale.total,
        )
        Payment.objects.create(sale=sale, tender="cash", amount=sale.total)
        StockMovement.objects.create(
            shop=self.shop, product=self.product, quantity_delta=-1,
            reason=StockMovement.Reason.SALE, sale=sale, actor=self.admin,
        )
        return sale

    def test_catalog_and_search_render_long_product_names_and_models(self):
        self.product.name = "Phone brand " + "A" * 180
        self.product.brand_model = "Model " + "B" * 180
        self.product.save(update_fields=["name", "brand_model"])

        for query in ("", self.product.name, self.product.brand_model,
                      self.product.sku, self.product.display_name):
            with self.subTest(query=query):
                response = self.client.get("/products/", {"q": query})
                self.assertContains(response, self.product.name)
                self.assertContains(response, self.product.brand_model)
                self.assertEqual([row.pk for row in response.context["products"]], [self.product.pk])
                self.assertEqual(response.context["products"][0].current_stock, 5)

    def test_catalog_search_and_suggestions_exclude_other_shops_and_deleted_items(self):
        other_shop = Shop.objects.create(name="Other catalog")
        Product.objects.create(shop=other_shop, name="Private product", brand_model="Secret model", sku="PRIVATE")
        Product.objects.create(shop=self.shop, name="Deleted product", brand_model="Archived model", sku="DELETED", is_active=False)

        for query in ("", "Private", "Secret model", "PRIVATE", "Deleted", "Archived model", "DELETED", "no match"):
            with self.subTest(query=query):
                response = self.client.get("/products/", {"q": query})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row.pk for row in response.context["products"]], [self.product.pk] if not query else [])
                self.assertEqual([row.pk for row in response.context["search_suggestions"]], [self.product.pk])

    def test_row_actions_and_routes_require_product_permission(self):
        self.assertContains(self.client.get("/products/"), self.edit_url)
        self.assertContains(self.client.get("/products/"), self.delete_url)
        self.member = self.staff
        listing = self.client.get("/products/")
        self.assertNotContains(listing, self.edit_url)
        self.assertNotContains(listing, self.delete_url)
        for url in (self.edit_url, self.delete_url):
            for method in (self.client.get, self.client.post):
                with self.subTest(url=url, method=method.__name__):
                    self.assertEqual(method(url).status_code, 403)
        self.staff.permissions = {"products_edit": True}
        self.assertContains(self.client.get("/products/"), self.edit_url)
        self.assertEqual(self.client.get(self.edit_url).status_code, 200)
        self.client.cookies.clear()
        self.assertRedirects(self.client.post(self.delete_url), "/sign-in/", fetch_redirect_response=False)
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)

    def test_other_shop_items_cannot_be_edited_or_deleted(self):
        other_shop = Shop.objects.create(name="Other shop")
        other = Product.objects.create(shop=other_shop, name="Other product")
        for action in ("edit", "delete"):
            url = f"/products/{other.pk}/{action}/"
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, self.data()).status_code, 404)
        other.refresh_from_db()
        self.assertEqual(other.name, "Other product")
        self.assertTrue(other.is_active)

    def test_edit_prefills_values_without_opening_stock(self):
        response = self.client.get(self.edit_url)
        form = response.context["form"]
        self.assertEqual(form["name"].value(), self.product.name)
        self.assertEqual(form["price"].value(), Decimal("100.00"))
        self.assertEqual(form["cost"].value(), Decimal("40.00"))
        self.assertNotIn("opening_quantity", form.fields)
        self.assertNotIn("tracking", form.fields)
        self.assertContains(response, "Save changes")

    def test_edit_preserves_stock_and_completed_receipts(self):
        from .models import AuditEvent

        sale = self.make_sale()
        line_before = list(sale.lines.values())
        movements_before = list(StockMovement.objects.values())
        response = self.client.post(self.edit_url, self.data(
            kind="service", tracking="serialized", opening_quantity="999", shop="999",
        ))
        self.assertRedirects(response, "/products/", fetch_redirect_response=False)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, "Updated cable")
        self.assertEqual(self.product.price, Decimal("150.00"))
        self.assertEqual(self.product.cost, Decimal("50.00"))
        self.assertEqual(self.product.kind, Product.Kind.STOCK)
        self.assertEqual(self.product.tracking, Product.Tracking.QUANTITY)
        self.assertEqual(self.product.shop, self.shop)
        self.assertEqual(self.product.on_hand, 4)
        self.assertEqual(list(sale.lines.values()), line_before)
        self.assertEqual(list(StockMovement.objects.values()), movements_before)
        event = AuditEvent.objects.get(event_type="catalog.product_updated")
        self.assertIn("price", event.details["changed_fields"])
        self.assertNotIn("50.00", json.dumps(event.details))

    def test_edit_keeps_serialized_phone_acquisition_and_identity(self):
        from .models import Device

        self.product.tracking = Product.Tracking.SERIALIZED
        self.product.is_second_hand = True
        self.product.save()
        device = Device.objects.create(
            shop=self.shop, product=self.product, imei="123456789012345", is_second_hand=True,
            condition=Device.Condition.GOOD, acquisition_source="Customer buy-in",
            acquisition_cost=Decimal("30.00"), acquisition_date=timezone.localdate(),
        )
        device_before = list(Device.objects.values())
        self.assertEqual(self.client.post(self.edit_url, self.data()).status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.tracking, Product.Tracking.SERIALIZED)
        self.assertTrue(self.product.is_second_hand)
        self.assertEqual(list(Device.objects.values()), device_before)
        self.assertTrue(Device.objects.filter(pk=device.pk, product=self.product).exists())

    def test_cost_permission_hides_and_protects_buying_price(self):
        self.staff.permissions = {"products_edit": True}
        self.member = self.staff
        response = self.client.get(self.edit_url)
        self.assertNotIn("cost", response.context["form"].fields)
        self.assertNotContains(response, 'name="cost"')
        self.assertNotContains(response, "40.00")
        self.assertEqual(self.client.post(self.edit_url, self.data(cost="0")).status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost, Decimal("40.00"))
        self.assertEqual(self.product.price, Decimal("150.00"))

    def test_invalid_sku_supplier_and_prices_show_field_errors(self):
        other_shop = Shop.objects.create(name="Outside shop")
        supplier = Supplier.objects.create(shop=other_shop, name="Outside supplier")
        Product.objects.create(shop=self.shop, name="Deleted product", sku="TAKEN", is_active=False)
        for changes, field in (
            ({"sku": "TAKEN"}, "sku"),
            ({"supplier": supplier.pk}, "supplier"),
            ({"price": "-1"}, "price"),
            ({"cost": "-1"}, "cost"),
            ({"name": ""}, "name"),
        ):
            with self.subTest(changes=changes):
                response = self.client.post(self.edit_url, self.data(**changes))
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.product.refresh_from_db()
                self.assertEqual(self.product.name, "USB cable")
                self.assertEqual(self.product.cost, Decimal("40.00"))

    def test_existing_archived_supplier_can_be_kept(self):
        supplier = Supplier.objects.create(shop=self.shop, name="Archived supplier", is_active=False)
        self.product.supplier = supplier
        self.product.save()
        self.assertEqual(self.client.post(self.edit_url, self.data(supplier=supplier.pk)).status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.supplier, supplier)

    def test_service_edits_cannot_introduce_stock_or_change_type(self):
        self.product.kind = Product.Kind.SERVICE
        self.product.save()
        before = list(StockMovement.objects.values())
        self.assertEqual(self.client.post(self.edit_url, self.data(
            kind="stock", opening_quantity="10", is_second_hand="on",
        )).status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.kind, Product.Kind.SERVICE)
        self.assertFalse(self.product.is_second_hand)
        self.assertIsNone(self.product.on_hand)
        self.assertEqual(list(StockMovement.objects.values()), before)

    def test_shared_create_form_preserves_opening_stock_and_cost_permissions(self):
        self.staff.permissions = {"products_edit": True}
        self.member = self.staff
        self.assertNotContains(self.client.get("/products/new/"), 'name="cost"')
        response = self.client.post("/products/new/", self.data(
            name="New item", sku="NEW-1", opening_quantity="7", cost="99.00",
        ))
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(sku="NEW-1")
        self.assertEqual(product.on_hand, 7)
        self.assertIsNone(product.cost)
        duplicate = self.client.post("/products/new/", self.data(sku="NEW-1"))
        self.assertIn("sku", duplicate.context["form"].errors)
        self.assertEqual(Product.objects.filter(sku="NEW-1").count(), 1)

    def test_delete_confirmation_preserves_history_and_retry_is_idempotent(self):
        from .models import AuditEvent

        sale = self.make_sale()
        line_before = list(sale.lines.values())
        payment_before = list(sale.payments.values())
        movements_before = list(StockMovement.objects.values())
        self.assertContains(self.client.get(self.delete_url), "Past receipts")
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)
        self.assertEqual(self.client.post(self.delete_url).status_code, 302)
        self.assertEqual(self.client.post(self.delete_url).status_code, 302)
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)
        self.assertEqual(list(sale.lines.values()), line_before)
        self.assertEqual(list(sale.payments.values()), payment_before)
        self.assertEqual(list(StockMovement.objects.values()), movements_before)
        self.assertEqual(AuditEvent.objects.filter(event_type="catalog.product_deleted").count(), 1)
        self.assertNotContains(self.client.get("/products/"), self.product.name)
        self.assertNotContains(self.client.get("/sell/"), self.product.name)
        self.assertEqual(self.client.get(self.edit_url).status_code, 404)
        self.assertEqual(self.client.post(self.edit_url, self.data()).status_code, 404)
        from .forms import StockReceiveForm
        self.assertNotIn(self.product, StockReceiveForm(shop=self.shop).fields["product"].queryset)
        stale_checkout = self.client.post("/sell/", {
            "request_key": uuid.uuid4(), "tender": "cash",
            "cart_json": json.dumps([{"product_id": self.product.pk, "quantity": 1}]),
        })
        self.assertEqual(stale_checkout.status_code, 404)
        self.assertEqual(Sale.objects.count(), 1)

    def test_delete_preserves_serialized_devices(self):
        from .models import Device

        device = Device.objects.create(shop=self.shop, product=self.product, imei="123456789012345")
        self.assertEqual(self.client.post(self.delete_url).status_code, 302)
        self.assertTrue(Device.objects.filter(pk=device.pk, product=self.product).exists())

    def test_delete_requires_csrf_and_refuses_unsupported_methods(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.cookies["supabase_access_token"] = "test-session"
        self.assertEqual(client.post(self.delete_url).status_code, 403)
        self.assertEqual(client.post(self.edit_url, self.data()).status_code, 403)
        self.assertEqual(self.client.delete(self.delete_url).status_code, 405)
        self.assertEqual(self.client.put(self.edit_url).status_code, 405)
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)

    def test_audit_failures_roll_back_edits_and_deletion(self):
        for url, data in ((self.edit_url, self.data()), (self.delete_url, {})):
            with self.subTest(url=url):
                with patch("shop.views.record_event", side_effect=RuntimeError("Audit unavailable")):
                    with self.assertRaises(RuntimeError):
                        self.client.post(url, data)
                self.product.refresh_from_db()
                self.assertTrue(self.product.is_active)
                self.assertEqual(self.product.name, "USB cable")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class BillPriceTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Bill prices", tax_rate=Decimal("18.000"))
        self.admin = Membership.objects.create(shop=self.shop, role=Membership.Role.ADMIN)
        self.staff = Membership.objects.create(shop=self.shop, role=Membership.Role.STAFF)
        self.product = Product.objects.create(
            shop=self.shop, name="USB cable", price=Decimal("100.00"), cost=Decimal("40.00"),
        )
        StockMovement.objects.create(
            shop=self.shop, product=self.product, quantity_delta=5,
            reason=StockMovement.Reason.OPENING, actor=self.admin,
        )

    def checkout(self, price=None, member=None, quantity=1, request_key=None, product=None, **extra):
        from django.contrib.messages.storage.fallback import FallbackStorage

        line = {"product_id": (product or self.product).pk, "quantity": quantity}
        if price is not None:
            line["unit_price"] = price
        line.update(extra)
        request = RequestFactory().post("/sell/", {
            "request_key": str(request_key or uuid.uuid4()),
            "cart_json": json.dumps([line]), "tender": "cash",
        })
        request.membership = member or self.admin
        request.session = {}
        request._messages = FallbackStorage(request)
        return point_of_sale(request)

    def test_negotiated_price_drives_receipt_payment_tax_and_audit(self):
        from .models import AuditEvent

        response = self.checkout("85.50", quantity=2)
        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        line = sale.lines.get()
        self.assertEqual(sale.subtotal, Decimal("171.00"))
        self.assertEqual(sale.tax_total, Decimal("30.78"))
        self.assertEqual(sale.total, Decimal("201.78"))
        self.assertEqual(line.unit_price, Decimal("85.50"))
        self.assertEqual(line.tax_amount, Decimal("30.78"))
        self.assertEqual(line.line_total, sale.total)
        self.assertEqual(sale.payments.get().amount, sale.total)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("100.00"))
        self.assertEqual(self.product.on_hand, 3)
        changes = AuditEvent.objects.get(event_type="sales.completed").details["price_changes"]
        self.assertEqual(changes[0]["catalog_price"], "100.00")
        self.assertEqual(changes[0]["unit_price"], "85.50")
        self.assertNotIn("cost", changes[0])

    def test_same_brand_models_are_distinct_in_checkout_and_receipt_snapshots(self):
        self.product.name = "BOAT"
        self.product.brand_model = "212"
        self.product.save()
        Product.objects.create(shop=self.shop, name="BOAT", brand_model="81", price=1100)
        request = RequestFactory().get("/sell/")
        request.membership = self.admin
        response = point_of_sale(request)
        self.assertContains(response, 'data-name="BOAT" data-model="212"')
        self.assertContains(response, 'data-name="BOAT" data-model="81"')
        self.assertContains(response, '<span class="pos-product-brand">BOAT</span> <span class="pos-product-model">212</span>', html=True)
        self.assertNotContains(response, 'class="product-dot"')
        self.assertEqual(self.checkout().status_code, 302)
        line = Sale.objects.get().lines.get()
        self.assertEqual(line.description, "BOAT 212")
        self.product.brand_model = "Updated model"
        self.product.save()
        line.refresh_from_db()
        self.assertEqual(line.description, "BOAT 212")

    def test_staff_need_price_permission_even_when_bypassing_the_ui(self):
        for value in ("50.00", "150.00"):
            with self.subTest(price=value):
                response = self.checkout(value, member=self.staff)
                self.assertContains(response, "do not have permission to edit bill prices")
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(self.product.on_hand, 5)
        self.staff.permissions = {"discounts": True}
        self.assertEqual(self.checkout("125.00", member=self.staff).status_code, 302)
        self.assertEqual(Sale.objects.get().lines.get().unit_price, Decimal("125.00"))

    def test_staff_can_sell_at_catalog_price_without_override_permission(self):
        self.assertEqual(self.checkout(member=self.staff).status_code, 302)
        self.assertEqual(self.checkout("100.00", member=self.staff).status_code, 302)
        self.assertEqual(Sale.objects.count(), 2)
        self.assertEqual(self.product.on_hand, 3)

    def test_invalid_prices_do_not_post_any_financial_or_inventory_records(self):
        for value in ("-0.01", "NaN", "Infinity", "-Infinity", "", "bad", "1.001", "10000000000.00", True, [], {}):
            with self.subTest(price=value):
                self.assertEqual(self.checkout(value).status_code, 200)
                self.assertFalse(Sale.objects.exists())
                self.assertFalse(Payment.objects.exists())
                self.assertEqual(StockMovement.objects.count(), 1)

    def test_oversized_bill_total_is_rejected_before_posting(self):
        response = self.checkout("9999999999.99")
        self.assertContains(response, "bill amount is too large")
        self.assertFalse(Sale.objects.exists())
        self.assertEqual(self.product.on_hand, 5)

    def test_free_item_has_zero_tax_and_payment(self):
        self.assertEqual(self.checkout("0.00").status_code, 302)
        sale = Sale.objects.get()
        self.assertEqual(sale.total, Decimal("0.00"))
        self.assertEqual(sale.lines.get().unit_price, Decimal("0.00"))
        self.assertEqual(sale.payments.get().amount, Decimal("0.00"))

    def test_price_override_retry_keeps_original_bill_and_posts_once(self):
        from .models import AuditEvent

        key = uuid.uuid4()
        self.assertEqual(self.checkout("80.00", request_key=key).status_code, 302)
        self.assertEqual(self.checkout("70.00", request_key=key).status_code, 302)
        sale = Sale.objects.get()
        self.assertEqual(sale.lines.get().unit_price, Decimal("80.00"))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(reason=StockMovement.Reason.SALE).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(event_type="sales.completed").count(), 1)

    def test_service_price_can_be_edited_without_stock_changes(self):
        service = Product.objects.create(shop=self.shop, name="Setup", kind=Product.Kind.SERVICE, price=200)
        self.assertEqual(self.checkout("175.00", quantity=2, product=service).status_code, 302)
        sale = Sale.objects.get()
        self.assertEqual(sale.subtotal, Decimal("350.00"))
        self.assertEqual(sale.total, Decimal("413.00"))
        self.assertEqual(sale.lines.get().unit_price, Decimal("175.00"))
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_serialized_price_keeps_device_cost_and_identity(self):
        from .models import Device

        self.product.tracking = Product.Tracking.SERIALIZED
        self.product.save()
        device = Device.objects.create(
            shop=self.shop, product=self.product, imei="123456789012345", acquisition_cost=Decimal("65.00"),
        )
        self.assertEqual(self.checkout("90.00", device_id=device.pk).status_code, 302)
        line = Sale.objects.get().lines.get()
        self.assertEqual(line.device_id, device.pk)
        self.assertEqual(line.unit_price, Decimal("90.00"))
        self.assertEqual(line.unit_cost, Decimal("65.00"))
        device.refresh_from_db()
        self.assertEqual(device.status, Device.Status.SOLD)
        self.assertEqual(device.acquisition_cost, Decimal("65.00"))

    def test_returns_refund_the_negotiated_amount_after_catalog_price_changes(self):
        from .models import SaleReturn
        from .views import returns

        self.checkout("80.00", quantity=2)
        sale = Sale.objects.get()
        self.product.price = Decimal("500.00")
        self.product.save()
        request = RequestFactory().post("/returns/", {
            "request_key": str(uuid.uuid4()), "receipt_number": sale.receipt_number,
            "line_id": sale.lines.get().pk, "quantity": "1", "reason": "Return",
            "refund_tender": "cash", "disposition": "sellable",
        })
        request.membership = self.admin
        self.assertEqual(returns(request).status_code, 302)
        self.assertEqual(SaleReturn.objects.get().refund_total, Decimal("94.40"))
        self.assertEqual(self.product.on_hand, 4)
        sale.refresh_from_db()
        self.assertEqual(sale.lines.get().unit_price, Decimal("80.00"))

    def test_half_penny_tax_rounding_matches_the_paid_price(self):
        self.shop.tax_rate = Decimal("5.000")
        self.shop.save()
        self.checkout("0.10")
        sale = Sale.objects.get()
        self.assertEqual(sale.tax_total, Decimal("0.01"))
        self.assertEqual(sale.total, Decimal("0.11"))

    def test_duplicate_lines_cannot_oversell_stock_or_repeat_an_imei(self):
        from .models import Device

        for tracking in (Product.Tracking.QUANTITY, Product.Tracking.SERIALIZED):
            with self.subTest(tracking=tracking):
                self.product.tracking = tracking
                self.product.save()
                line = {"product_id": self.product.pk, "quantity": 3, "unit_price": "80.00"}
                if tracking == Product.Tracking.SERIALIZED:
                    device = Device.objects.create(shop=self.shop, product=self.product, imei="123456789012345")
                    line.update(quantity=1, device_id=device.pk)
                request = RequestFactory().post("/sell/", {
                    "request_key": str(uuid.uuid4()), "tender": "cash",
                    "cart_json": json.dumps([line, line]),
                })
                request.membership = self.admin
                self.assertEqual(point_of_sale(request).status_code, 200)
                self.assertFalse(Sale.objects.exists())
                self.assertFalse(Payment.objects.exists())
                self.assertEqual(StockMovement.objects.count(), 1)
                if tracking == Product.Tracking.SERIALIZED:
                    device.refresh_from_db()
                    self.assertEqual(device.status, Device.Status.IN_STOCK)

    def test_error_page_preserves_draft_for_correction_and_permission_controls_editor(self):
        from django.shortcuts import render

        with patch("shop.views.render", wraps=render) as renderer:
            self.checkout("1.001")
            context = renderer.call_args.args[2]
            self.assertEqual(context["cart_payload"][0]["unit_price"], "1.001")
            self.assertTrue(context["can_edit_prices"])
        request = RequestFactory().get("/sell/")
        request.membership = self.staff
        self.assertNotContains(point_of_sale(request), 'data-edit-prices="true"')
        self.staff.permissions = {"discounts": True}
        self.assertContains(point_of_sale(request), 'data-edit-prices="true"')
