from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from .models import AuditEvent, Expense, Membership, Payment, Product, Purchase, Sale, Shop, StockMovement, Supplier


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class EntryManagementTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Shop")
        self.admin = Membership.objects.create(shop=self.shop, role="admin")
        self.staff = Membership.objects.create(shop=self.shop, role="staff", email="staff@example.com")
        self.member = self.admin
        self.supplier = Supplier.objects.create(shop=self.shop, name="Supplier")
        self.product = Product.objects.create(shop=self.shop, name="BOAT", brand_model="212", price=100)
        self.purchase = Purchase.objects.create(shop=self.shop, supplier=self.supplier, created_by=self.admin)
        self.movement = StockMovement.objects.create(shop=self.shop, product=self.product, quantity_delta=5, reason="purchase", purchase=self.purchase, actor=self.admin)
        self.expense = Expense.objects.create(shop=self.shop, category="rent", amount=100, created_by=self.admin)
        self.client.cookies["supabase_access_token"] = "test-session"
        for target, kwargs in (
            ("shop.middleware.verify_access_token", {"return_value": {"sub": "test"}}),
            ("shop.middleware.resolve_membership", {"side_effect": lambda *args: self.member}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def url(self, kind, obj, action="edit"):
        return f"/entries/{kind}/{obj.pk}/{action}/"

    def test_supplier_edit_delete_scope_permissions_and_audit(self):
        url = self.url("supplier", self.supplier)
        self.member = self.staff
        self.assertEqual(self.client.post(url, {"name": "Changed", "reason": "Fix"}).status_code, 403)
        self.member = self.admin
        self.assertEqual(self.client.post(url, {"name": "Changed", "phone": "123", "email": "", "reason": "Fix"}).status_code, 302)
        event = AuditEvent.objects.get(event_type="supplier.entry_edited")
        self.assertEqual(event.details["before"]["name"], "Supplier")
        self.assertEqual(event.details["after"]["name"], "Changed")
        self.assertEqual(self.client.post(self.url("supplier", self.supplier, "delete"), {"reason": "Closed"}).status_code, 302)
        self.supplier.refresh_from_db()
        self.assertFalse(self.supplier.is_active)
        self.assertEqual(self.purchase.supplier_id, self.supplier.pk)
        other = Supplier.objects.create(shop=Shop.objects.create(name="Other"), name="Outside")
        self.assertEqual(self.client.post(self.url("supplier", other, "delete"), {"reason": "No"}).status_code, 404)

    def test_expense_edit_reverses_original_and_links_replacement_once(self):
        data = {"category": "rent", "amount": "120", "expense_date": "2026-11-22", "payment_method": "cash", "description": "Correct rent", "reference": "", "reason": "Wrong amount"}
        url = self.url("expense", self.expense)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.amount, Decimal("100"))
        self.assertEqual(self.expense.reversal.amount, self.expense.amount)
        event = AuditEvent.objects.get(event_type="expense.entry_edited")
        self.assertEqual(Expense.objects.get(pk=event.details["replacement_id"]).amount, Decimal("120"))
        self.assertEqual(Expense.objects.count(), 3)

    def test_purchase_reverse_is_atomic_idempotent_and_preserves_original(self):
        url = self.url("purchase", self.purchase, "delete")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.product.on_hand, 5)
        self.assertEqual(self.client.post(url, {"reason": "Duplicate"}).status_code, 302)
        self.assertEqual(self.client.post(url, {"reason": "Duplicate"}).status_code, 200)
        self.assertEqual(self.product.on_hand, 0)
        self.movement.refresh_from_db()
        self.assertEqual(self.movement.quantity_delta, 5)
        self.assertEqual(Purchase.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_purchase_quantity_edit_records_difference_and_rejects_negative_stock(self):
        url = self.url("purchase", self.purchase)
        data = {"supplier": self.supplier.pk, "reference": "Corrected", "quantity": 3, "reason": "Count"}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.product.on_hand, 3)
        self.movement.refresh_from_db()
        self.assertEqual(self.movement.quantity_delta, 5)
        second = Purchase.objects.create(shop=self.shop, created_by=self.admin)
        StockMovement.objects.create(shop=self.shop, product=self.product, purchase=second, quantity_delta=10, reason="purchase", actor=self.admin)
        StockMovement.objects.create(shop=self.shop, product=self.product, quantity_delta=-12, reason="sale", actor=self.admin)
        self.assertEqual(self.client.post(self.url("purchase", second, "delete"), {"reason": "Bad"}).status_code, 200)
        self.assertEqual(self.product.on_hand, 1)
        self.assertFalse(AuditEvent.objects.filter(target_type="purchase", target_id=str(second.pk)).exists())

    def test_stock_edit_preserves_original_and_cannot_reverse_purchase_corrections(self):
        movement = StockMovement.objects.create(shop=self.shop, product=self.product, quantity_delta=3, reason="adjustment", actor=self.admin)
        self.assertEqual(self.client.post(self.url("movement", movement), {"quantity_delta": 1, "reason": "Wrong count"}).status_code, 302)
        self.assertEqual(self.product.on_hand, 6)
        movement.refresh_from_db()
        self.assertEqual(movement.quantity_delta, 3)
        self.assertEqual(self.client.post(self.url("movement", self.movement, "delete"), {"reason": "No"}).status_code, 403)

    def test_staff_edit_delete_retains_identity_and_protects_admins(self):
        self.assertEqual(self.client.post(self.url("member", self.staff), {"display_name": "Cashier", "permissions": ["products_edit"], "reason": "Update"}).status_code, 302)
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.display_name, "Cashier")
        self.assertTrue(self.staff.can("products_edit"))
        self.assertEqual(self.client.post(self.url("member", self.staff, "delete"), {"reason": "Left"}).status_code, 302)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_active)
        self.assertEqual(self.staff.email, "staff@example.com")
        self.assertEqual(self.client.post(self.url("member", self.admin, "delete"), {"reason": "No"}).status_code, 403)

    def test_pages_render_model_names_and_management_actions(self):
        for url in ("/suppliers/", "/expenses/", "/team/", "/stock/adjustments/"):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(self.client.get("/suppliers/"), "BOAT 212")
        self.assertContains(self.client.get("/stock/receive/"), "BOAT 212")
        self.assertContains(self.client.get("/stock/adjustments/"), "BOAT 212")

    def test_audit_failure_rolls_back_supplier_edit(self):
        with patch("shop.entry_management.record_event", side_effect=RuntimeError("Audit failed")):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url("supplier", self.supplier), {"name": "Changed", "reason": "Fix"})
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.name, "Supplier")

    def test_post_requires_reason_and_csrf(self):
        from django.test import Client
        url = self.url("purchase", self.purchase, "delete")
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.product.on_hand, 5)
        client = Client(enforce_csrf_checks=True)
        client.cookies["supabase_access_token"] = "test-session"
        self.assertEqual(client.post(url, {"reason": "Delete"}).status_code, 403)

    def test_void_and_reissue_links_new_sale_and_blocks_second_reissue(self):
        import json
        import uuid
        response = self.client.post("/sell/", {"request_key": str(uuid.uuid4()), "cart_json": json.dumps([{"product_id": self.product.pk, "quantity": 1}]), "tender": "cash"})
        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        self.assertEqual(self.client.post(self.url("sale", sale), {"customer_name": "Correct name", "customer_phone": "123", "reason": "Typo"}).status_code, 302)
        sale.refresh_from_db()
        self.assertEqual(sale.customer_name, "Correct name")
        response = self.client.post(f"/sales/{sale.pk}/void/", {"reason": "Wrong amount", "reissue": "1"})
        self.assertEqual(response.status_code, 302)
        draft = self.client.get(response.url)
        self.assertEqual(draft.context["cart_payload"][0]["product_id"], self.product.pk)
        payload = {"request_key": str(uuid.uuid4()), "source_sale": str(sale.pk), "cart_json": json.dumps([{"product_id": self.product.pk, "quantity": 1, "unit_price": "80.00"}]), "tender": "cash"}
        self.assertEqual(self.client.post("/sell/", payload).status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(event_type="sales.completed", details__replaces_sale=str(sale.pk)).exists())
        payload["request_key"] = str(uuid.uuid4())
        self.assertEqual(self.client.post("/sell/", payload).status_code, 200)
        self.assertEqual(Sale.objects.count(), 2)
        self.assertEqual(self.product.on_hand, 4)

    def test_serialized_purchase_reversal_blocks_moved_phone_and_rolls_back_all(self):
        from .models import Device
        phone = Product.objects.create(shop=self.shop, name="Phone", brand_model="128GB", tracking="serialized")
        purchase = Purchase.objects.create(shop=self.shop, created_by=self.admin)
        first = Device.objects.create(shop=self.shop, product=phone, imei="111111111111111")
        sold = Device.objects.create(shop=self.shop, product=phone, imei="222222222222222", status="sold")
        for device in (first, sold):
            StockMovement.objects.create(shop=self.shop, product=phone, device=device, purchase=purchase, quantity_delta=1, reason="purchase", actor=self.admin)
        self.assertEqual(self.client.post(self.url("purchase", purchase, "delete"), {"reason": "Wrong receipt"}).status_code, 200)
        first.refresh_from_db()
        self.assertEqual(first.status, "in_stock")
        self.assertEqual(StockMovement.objects.filter(purchase=purchase).count(), 2)
        self.assertFalse(AuditEvent.objects.filter(target_type="purchase", target_id=str(purchase.pk)).exists())

    def test_staff_cannot_manage_entries_without_permissions(self):
        self.member = self.staff
        for kind, obj in (("supplier", self.supplier), ("expense", self.expense), ("purchase", self.purchase), ("member", self.staff), ("movement", self.movement)):
            for action in ("edit", "delete"):
                with self.subTest(kind=kind, action=action):
                    self.assertEqual(self.client.get(self.url(kind, obj, action)).status_code, 403)
                    self.assertEqual(self.client.post(self.url(kind, obj, action), {"reason": "No"}).status_code, 403)

    def test_low_stock_shortcut_preselects_product_and_enforces_scope(self):
        self.product.reorder_level = 10
        self.product.supplier = self.supplier
        self.product.save()
        url = f"/stock/receive/?product={self.product.pk}"
        self.assertContains(self.client.get("/"), f"/stock/{self.product.pk}/quick/")
        response = self.client.get(url)
        self.assertEqual(response.context["form"]["product"].value(), self.product.pk)
        self.assertEqual(response.context["form"]["supplier"].value(), self.supplier.pk)
        self.assertEqual(self.product.on_hand, 5)
        other = Product.objects.create(shop=Shop.objects.create(name="Other"), name="Other")
        self.assertEqual(self.client.get(f"/stock/receive/?product={other.pk}").status_code, 404)
        self.assertEqual(self.client.get("/stock/receive/?product=invalid").status_code, 400)
        self.member = self.staff
        self.assertNotContains(self.client.get("/"), f"/stock/{self.product.pk}/quick/")
        self.assertEqual(self.client.get(url).status_code, 403)


    def test_quick_stock_add_remove_retry_and_negative_stock(self):
        import uuid
        url = f"/stock/{self.product.pk}/quick/"
        data = {"quantity": "2", "action": "add", "request_key": str(uuid.uuid4())}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.product.on_hand, 7)
        self.assertEqual(Purchase.objects.count(), 2)
        data.update(action="remove", request_key=str(uuid.uuid4()))
        self.client.post(url, data)
        self.assertEqual(self.product.on_hand, 5)
        data.update(quantity="6", request_key=str(uuid.uuid4()))
        self.client.post(url, data)
        self.assertEqual(self.product.on_hand, 5)
        self.member = self.staff
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.staff.permissions = {"purchases": True}
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 405)
