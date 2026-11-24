from django import forms
from django.db.models import Q

from .models import Expense, Membership, Product, Shop, Supplier


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ["name", "sku", "category", "brand_model", "supplier", "kind", "is_second_hand", "price", "cost", "reorder_level"]
        labels = {
            "sku": "SKU",
            "brand_model": "Model",
            "kind": "Type",
            "is_second_hand": "Second-hand",
            "price": "Selling price",
            "cost": "Buying price",
            "reorder_level": "Low-stock alert (pieces)",
        }
        help_texts = {
            "reorder_level": "Show an alert when stock reaches this quantity.",
        }
        widgets = {
            "name": forms.Textarea(attrs={"rows": 2}),
            "brand_model": forms.Textarea(attrs={"rows": 3}),
            "price": forms.NumberInput(attrs={"step": "0.01", "placeholder": "0"}),
            "cost": forms.NumberInput(attrs={"step": "0.01", "placeholder": "0"}),
            "reorder_level": forms.NumberInput(attrs={"min": "0", "step": "1", "placeholder": "0"}),
        }

    def __init__(self, *args, shop, can_view_costs=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.shop = shop
        for field_name in ("price", "cost", "reorder_level"):
            self.fields[field_name].initial = None
        if not can_view_costs:
            self.fields.pop("cost")
        self.fields["reorder_level"].required = False
        suppliers = Supplier.objects.filter(shop=shop, is_active=True)
        if self.instance.pk and self.instance.supplier_id:
            suppliers = Supplier.objects.filter(shop=shop).filter(
                Q(is_active=True) | Q(pk=self.instance.supplier_id)
            )
        self.fields["supplier"].queryset = suppliers
        self.fields["supplier"].required = False
        self.fields["supplier"].empty_label = "Choose a saved supplier (optional)"
        if self.instance.pk:
            self.fields["kind"].disabled = True
            self.fields["kind"].help_text = "Type stays the same to preserve stock and sales history."
            if self.instance.tracking == Product.Tracking.SERIALIZED:
                self.fields["is_second_hand"].disabled = True
                self.fields["is_second_hand"].help_text = "Individual phone records keep their condition and acquisition details."
            return
        self.fields["tracking"] = forms.ChoiceField(
            choices=[(Product.Tracking.QUANTITY, Product.Tracking.QUANTITY.label)],
            initial=Product.Tracking.QUANTITY,
            required=False,
            widget=forms.HiddenInput(),
        )
        self.fields["opening_quantity"] = forms.IntegerField(
            min_value=0,
            required=False,
            initial=None,
            label="Quantity",
            help_text="Starting quantity for this item.",
            widget=forms.NumberInput(attrs={"min": "0", "step": "1", "placeholder": "0"}),
        )

    def clean_sku(self):
        sku = self.cleaned_data["sku"]
        if sku and Product.objects.filter(shop=self.shop, sku=sku).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This SKU is already used by another item, including deleted items. Choose a different SKU.")
        return sku

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price < 0:
            raise forms.ValidationError("Selling price cannot be negative.")
        return price

    def clean_cost(self):
        cost = self.cleaned_data["cost"]
        if cost is not None and cost < 0:
            raise forms.ValidationError("Buying price cannot be negative.")
        return cost

    def clean(self):
        data = super().clean()
        if self.instance.pk:
            if data.get("kind") == Product.Kind.SERVICE:
                data["is_second_hand"] = False
            data["reorder_level"] = data.get("reorder_level") or 0
            return data
        if data.get("kind") == Product.Kind.SERVICE:
            data["tracking"] = Product.Tracking.QUANTITY
            data["is_second_hand"] = False
        data["tracking"] = Product.Tracking.QUANTITY
        data["reorder_level"] = data.get("reorder_level") or 0
        opening_quantity = data.get("opening_quantity") or 0
        if data.get("kind") == Product.Kind.SERVICE and opening_quantity:
            self.add_error("opening_quantity", "Services do not have stock pieces.")
        data["opening_quantity"] = opening_quantity
        return data

class CheckoutLinePriceForm(forms.Form):
    unit_price = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, label="Selling price",
    )


class StockReceiveForm(forms.Form):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), required=False, empty_label="Cash purchase / supplier not listed", label="Supplier")
    product = forms.ModelChoiceField(queryset=Product.objects.none(), label="Product")
    quantity = forms.IntegerField(min_value=1, initial=1, label="Quantity received (pieces)")
    unit_cost = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False, label="Buying price per phone / piece")
    reference = forms.CharField(max_length=80, required=False, label="Purchase reference")
    imeis = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}), label="IMEI / serial numbers", help_text="For a serialized phone, enter one IMEI or serial per line.")
    is_second_hand = forms.BooleanField(required=False, label="These are second-hand phones")
    condition = forms.ChoiceField(choices=[("like_new", "Like new"), ("good", "Good"), ("fair", "Fair"), ("parts", "For parts")], required=False)
    condition_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Condition notes")
    included_accessories = forms.CharField(required=False, label="Included accessories")
    acquisition_source = forms.CharField(max_length=120, required=False, label="Bought from")

    def __init__(self, *args, shop, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.filter(shop=shop, is_active=True)
        self.fields["product"].queryset = Product.objects.filter(shop=shop, is_active=True, kind=Product.Kind.STOCK)
        self.fields["product"].label_from_instance = lambda product: (
            f"{product.display_name} · {'IMEI / serial' if product.tracking == Product.Tracking.SERIALIZED else 'Quantity'}"
            f"{' · Second-hand' if product.is_second_hand else ''}"
        )

    def clean(self):
        data = super().clean()
        product = data.get("product")
        imeis = [value.strip() for value in (data.get("imeis") or "").splitlines() if value.strip()]
        if product and product.tracking == Product.Tracking.SERIALIZED:
            if len(imeis) != data.get("quantity"):
                self.add_error("imeis", "Enter exactly one unique IMEI or serial number for each phone received.")
            if len(set(imeis)) != len(imeis):
                self.add_error("imeis", "Each IMEI or serial number must be unique.")
            if data.get("is_second_hand") and not data.get("condition"):
                self.add_error("condition", "Choose the condition of each second-hand phone.")
            if product.is_second_hand and not data.get("is_second_hand"):
                self.add_error("is_second_hand", "This product is marked second-hand. Select this option and enter its condition.")
            if not data.get("is_second_hand") and any(data.get(field) for field in ("condition", "condition_notes", "included_accessories", "acquisition_source")):
                self.add_error("is_second_hand", "Select this option to record second-hand phone details.")
        elif product and imeis:
            self.add_error("imeis", "IMEI numbers are only needed for serialized products.")
        data["imei_list"] = imeis
        return data


STAFF_PERMISSION_OPTIONS = [
    ("products_edit", "Add, edit, and delete products"),
    ("purchases", "Receive stock and purchase phones"),
    ("view_sales", "View all sales"),
    ("view_costs", "View purchase costs"),
    ("stock_adjust", "Adjust stock"),
    ("discounts", "Edit bill prices and apply discounts"),
    ("refunds", "Process returns and refunds"),
    ("voids", "Void sales"),
    ("exports", "Export reports"),
    ("expenses", "Record and view shop expenses"),
]


class StaffAccountForm(forms.Form):
    email = forms.EmailField(label="Staff email")
    role = forms.ChoiceField(
        choices=Membership.Role.choices,
        initial=Membership.Role.STAFF,
        label="Account type",
    )
    password = forms.CharField(
        label="Temporary password",
        min_length=8,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}, render_value=False),
        help_text="Give this password to the staff member and ask them to change it after signing in.",
    )
    confirm_password = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}, render_value=False),
    )
    permissions = forms.MultipleChoiceField(choices=STAFF_PERMISSION_OPTIONS, required=False, widget=forms.CheckboxSelectMultiple, label="Extra access")

    def clean(self):
        data = super().clean()
        password = data.get("password")
        confirm_password = data.get("confirm_password")
        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "The passwords do not match.")
        return data


class StaffPermissionsForm(forms.Form):
    permissions = forms.MultipleChoiceField(choices=STAFF_PERMISSION_OPTIONS, required=False, widget=forms.CheckboxSelectMultiple, label="Staff permissions")


class ShopSettingsForm(forms.ModelForm):
    class Meta:
        model = Shop
        fields = ["name", "address", "phone", "email", "currency", "timezone", "tax_rate", "receipt_footer"]
        labels = {"name": "Shop name", "address": "Shop address", "phone": "Shop mobile number", "email": "Shop email", "currency": "Currency code", "timezone": "Time zone", "tax_rate": "Tax rate (%)", "receipt_footer": "Receipt note"}
        help_texts = {"address": "Printed below your shop name on new bills.", "phone": "Printed on new bills, including the 80 mm thermal receipt.", "email": "Printed on new bills when provided.", "currency": "Use a three-letter code, for example INR, USD, or EUR.", "tax_rate": "Enter 0 if tax is not configured. The rate is applied to new sales.", "receipt_footer": "A short note printed at the bottom of receipts."}

    def clean_currency(self):
        value = self.cleaned_data["currency"].strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise forms.ValidationError("Enter a three-letter currency code, such as INR or USD.")
        return value

    def clean_tax_rate(self):
        value = self.cleaned_data["tax_rate"]
        if value < 0 or value > 100:
            raise forms.ValidationError("Enter a tax rate between 0 and 100.")
        return value

    def clean_timezone(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        value = self.cleaned_data["timezone"].strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise forms.ValidationError("Enter a valid time zone, such as Asia/Kolkata.") from None
        return value


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "phone", "email"]
        labels = {"name": "Supplier name", "phone": "Phone number", "email": "Email address"}


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["category", "amount", "expense_date", "payment_method", "description", "reference"]
        labels = {"category": "Expense type", "amount": "Amount", "expense_date": "Date paid", "payment_method": "Paid by", "description": "What was this for?", "reference": "Receipt or reference number"}
        widgets = {"amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}), "expense_date": forms.DateInput(attrs={"type": "date"})}

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Enter an amount greater than zero.")
        return amount


class ExpenseReversalForm(forms.Form):
    reason = forms.CharField(max_length=200, label="Why are you correcting this expense?")


class ProductImportForm(forms.Form):
    csv_file = forms.FileField(label="Product CSV file")

    def clean_csv_file(self):
        file = self.cleaned_data["csv_file"]
        if file.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Choose a CSV file smaller than 5 MB.")
        if not file.name.lower().endswith(".csv"):
            raise forms.ValidationError("Choose a .csv file.")
        return file


class QuantityStocktakeForm(forms.Form):
    REASONS = [("count", "Physical count correction"), ("damage", "Damaged items"), ("loss", "Missing or lost items"), ("found", "Found stock"), ("other", "Other correction")]
    product = forms.ModelChoiceField(queryset=Product.objects.none(), label="Product")
    counted_quantity = forms.IntegerField(min_value=0, label="Physical pieces counted", help_text="Enter the total number of pieces you counted.")
    reason = forms.ChoiceField(choices=REASONS)
    note = forms.CharField(max_length=200, required=False, label="Additional note")

    def __init__(self, *args, shop, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(shop=shop, is_active=True, kind=Product.Kind.STOCK, tracking=Product.Tracking.QUANTITY)

    def clean(self):
        data = super().clean()
        if data.get("reason") == "damage" and data.get("counted_quantity") is None:
            self.add_error("counted_quantity", "Enter the physical count after removing damaged items.")
        return data
