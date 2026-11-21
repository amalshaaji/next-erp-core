import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone as dj_timezone


class Shop(models.Model):
    name = models.CharField(max_length=160)
    address = models.CharField(max_length=240, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    currency = models.CharField(max_length=3, default="INR")
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0"))
    receipt_footer = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        STAFF = "staff", "Staff"

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="memberships")
    supabase_user_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    display_name = models.CharField(max_length=160, blank=True)
    email = models.EmailField(blank=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STAFF)
    permissions = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    def can(self, capability):
        return self.role == self.Role.ADMIN or bool(self.permissions.get(capability, False))

    @property
    def can_costs(self):
        return self.can("view_costs")

    @property
    def can_edit_products(self):
        return self.can("products_edit")

    @property
    def can_receive_purchases(self):
        return self.can("purchases")

    @property
    def can_refunds(self):
        return self.can("refunds")

    @property
    def can_reports(self):
        return self.can("view_sales")

    @property
    def can_expenses(self):
        return self.can("expenses")

    @property
    def can_exports(self):
        return self.can("exports")

    @property
    def can_stock_adjust(self):
        return self.can("stock_adjust")

    @property
    def can_voids(self):
        return self.can("voids")

    def __str__(self):
        return self.display_name or self.email or self.supabase_user_id


class StaffInvitation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Invitation sent"
        ACCEPTED = "accepted", "Joined"
        REVOKED = "revoked", "Revoked"

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="staff_invitations")
    email = models.EmailField()
    supabase_user_id = models.CharField(max_length=100, blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_by = models.ForeignKey(Membership, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=dj_timezone.now)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["shop", "email"], condition=models.Q(status="pending"), name="unique_pending_staff_invite")]
        ordering = ["-created_at"]


class Product(models.Model):
    class Kind(models.TextChoices):
        STOCK = "stock", "Stock product"
        SERVICE = "service", "Service"

    class Tracking(models.TextChoices):
        QUANTITY = "quantity", "Quantity"
        SERIALIZED = "serialized", "IMEI / serial"

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="products")
    name = models.CharField(max_length=180)
    sku = models.CharField(max_length=80, blank=True)
    category = models.CharField(max_length=100, blank=True)
    brand_model = models.CharField(max_length=160, blank=True)
    supplier = models.ForeignKey("Supplier", null=True, blank=True, on_delete=models.SET_NULL, related_name="products")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.STOCK)
    tracking = models.CharField(max_length=12, choices=Tracking.choices, default=Tracking.QUANTITY)
    is_second_hand = models.BooleanField(default=False)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    reorder_level = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["shop", "sku"], condition=~models.Q(sku=""), name="unique_shop_sku")]

    def __str__(self):
        return self.name

    @property
    def on_hand(self):
        if self.kind == self.Kind.SERVICE:
            return None
        if self.tracking == self.Tracking.SERIALIZED:
            return self.devices.filter(status=Device.Status.IN_STOCK).count()
        return self.movements.aggregate(total=models.Sum("quantity_delta"))["total"] or 0


class Device(models.Model):
    class Status(models.TextChoices):
        IN_STOCK = "in_stock", "In stock"
        SOLD = "sold", "Sold"
        INSPECTION = "inspection", "Inspection"
        DAMAGED = "damaged", "Damaged"
        WRITTEN_OFF = "written_off", "Written off"

    class Condition(models.TextChoices):
        LIKE_NEW = "like_new", "Like new"
        GOOD = "good", "Good"
        FAIR = "fair", "Fair"
        PARTS = "parts", "For parts"

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="devices")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="devices")
    imei = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.IN_STOCK)
    is_second_hand = models.BooleanField(default=False)
    condition = models.CharField(max_length=12, choices=Condition.choices, blank=True)
    condition_notes = models.TextField(blank=True)
    included_accessories = models.CharField(max_length=240, blank=True)
    acquisition_source = models.CharField(max_length=120, blank=True)
    acquisition_date = models.DateField(null=True, blank=True)
    acquisition_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    inspection_status = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["shop", "imei"], name="unique_shop_imei")]
        ordering = ["imei"]

    def __str__(self):
        return f"{self.imei} · {self.product.name}"


class Supplier(models.Model):
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="suppliers")
    name = models.CharField(max_length=160)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Expense(models.Model):
    class Category(models.TextChoices):
        RENT = "rent", "Rent"
        ELECTRICITY = "electricity", "Electricity"
        INTERNET = "internet", "Internet / phone"
        SALARIES = "salaries", "Salaries"
        REPAIRS = "repairs", "Repairs & maintenance"
        TRANSPORT = "transport", "Transport"
        SUPPLIES = "supplies", "Shop supplies"
        OTHER = "other", "Other"

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="expenses")
    category = models.CharField(max_length=16, choices=Category.choices)
    description = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    expense_date = models.DateField(default=dj_timezone.localdate)
    payment_method = models.CharField(max_length=16, choices=[("cash", "Cash"), ("upi", "UPI"), ("card", "Card"), ("bank", "Bank transfer"), ("other", "Other")], default="cash")
    reference = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="recorded_expenses")
    reversal_of = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal")
    reversal_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ["-expense_date", "-created_at"]
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="expense_amount_positive")]


class Purchase(models.Model):
    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PARTIAL = "partial", "Partially received"

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL)
    reference = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RECEIVED)
    created_by = models.ForeignKey(Membership, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=dj_timezone.now)


class Sale(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    idempotency_key = models.UUIDField(null=True, blank=True)
    receipt_number = models.CharField(max_length=32)
    cashier = models.ForeignKey(Membership, on_delete=models.PROTECT)
    shop_name_snapshot = models.CharField(max_length=160, default="")
    shop_address_snapshot = models.CharField(max_length=240, blank=True)
    shop_phone_snapshot = models.CharField(max_length=40, blank=True)
    shop_email_snapshot = models.EmailField(blank=True)
    currency = models.CharField(max_length=3, default="INR")
    tax_rate_snapshot = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0"))
    receipt_footer_snapshot = models.CharField(max_length=240, blank=True)
    customer_name = models.CharField(max_length=160, blank=True)
    customer_phone = models.CharField(max_length=40, blank=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    tax_total = models.DecimalField(max_digits=12, decimal_places=2)
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["shop", "receipt_number"], name="unique_shop_receipt"),
            models.UniqueConstraint(fields=["shop", "idempotency_key"], condition=models.Q(idempotency_key__isnull=False), name="unique_shop_sale_idempotency"),
        ]
        ordering = ["-created_at"]


class SaleVoid(models.Model):
    sale = models.OneToOneField(Sale, on_delete=models.PROTECT, related_name="void_record")
    created_by = models.ForeignKey(Membership, on_delete=models.PROTECT)
    reason = models.CharField(max_length=240)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ["-created_at"]


class ImportBatch(models.Model):
    token = models.CharField(max_length=64, unique=True)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    created_by = models.ForeignKey(Membership, on_delete=models.PROTECT)
    rows_imported = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=dj_timezone.now)


class AuditEvent(models.Model):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="audit_events")
    actor = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.PROTECT)
    event_type = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=100, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["shop", "created_at"]), models.Index(fields=["shop", "event_type", "created_at"])]


class SaleLine(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.PROTECT)
    description = models.CharField(max_length=180)
    item_kind = models.CharField(max_length=10, choices=Product.Kind.choices, default=Product.Kind.STOCK)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    line_total = models.DecimalField(max_digits=12, decimal_places=2)


class SaleReturn(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="returns")
    created_by = models.ForeignKey(Membership, on_delete=models.PROTECT)
    reason = models.CharField(max_length=240)
    refund_total = models.DecimalField(max_digits=12, decimal_places=2)
    refund_tender = models.CharField(max_length=32)
    created_at = models.DateTimeField(default=dj_timezone.now)

    class Meta:
        ordering = ["-created_at"]


class ReturnLine(models.Model):
    class Disposition(models.TextChoices):
        SELLABLE = "sellable", "Return to sellable stock"
        INSPECTION = "inspection", "Hold for inspection"
        DAMAGED = "damaged", "Damaged / write off"
        SERVICE = "service", "Service refund"

    return_record = models.ForeignKey(SaleReturn, on_delete=models.PROTECT, related_name="lines")
    sale_line = models.ForeignKey(SaleLine, on_delete=models.PROTECT, related_name="return_lines")
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    disposition = models.CharField(max_length=16, choices=Disposition.choices)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax_refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))


class Payment(models.Model):
    class Kind(models.TextChoices):
        RECEIVED = "received", "Payment received"
        REFUND = "refund", "Refund paid"
        VOID = "void", "Payment reversed on void"

    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="payments")
    return_record = models.ForeignKey(SaleReturn, null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    void_record = models.ForeignKey(SaleVoid, null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.RECEIVED)
    tender = models.CharField(max_length=32)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=100, blank=True)


class StockMovement(models.Model):
    class Reason(models.TextChoices):
        OPENING = "opening", "Opening stock"
        PURCHASE = "purchase", "Purchase received"
        SALE = "sale", "Sale"
        RETURN = "return", "Return"
        VOID = "void", "Sale voided"
        ADJUSTMENT = "adjustment", "Adjustment"

    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="movements")
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.PROTECT)
    quantity_delta = models.IntegerField()
    reason = models.CharField(max_length=16, choices=Reason.choices)
    note = models.CharField(max_length=240, blank=True)
    sale = models.ForeignKey(Sale, null=True, blank=True, on_delete=models.PROTECT)
    purchase = models.ForeignKey(Purchase, null=True, blank=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(Membership, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=dj_timezone.now)
