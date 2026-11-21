import json
import uuid
import csv
import secrets
import logging
from django.conf import settings
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib import messages
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Exists, Max, Min, OuterRef, Prefetch, Sum
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .decorators import permission_required
from .forms import CheckoutLinePriceForm, ExpenseForm, ExpenseReversalForm, STAFF_PERMISSION_OPTIONS, ProductForm, ProductImportForm, QuantityStocktakeForm, ShopSettingsForm, StaffAccountForm, StaffPermissionsForm, StockReceiveForm, SupplierForm
from .models import AuditEvent, Device, Expense, ImportBatch, Membership, Payment, Product, Purchase, ReturnLine, Sale, SaleLine, SaleReturn, SaleVoid, Shop, StockMovement, Supplier
from .services.supabase_api import SupabaseApiError, create_user
from .services.reporting import build_daily_financials, build_report, calculate_financials
from .services.imports import preview_product_csv
from .services.audit import record_event
from .services.auth import resolve_membership, verify_access_token


CENT = Decimal("0.01")
MAX_MONEY = Decimal("9999999999.99")
logger = logging.getLogger(__name__)


def sign_in(request):
    if request.membership:
        return redirect("dashboard")
    return render(request, "shop/sign_in.html")


@csrf_exempt
def auth_session(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    token = request.POST.get("access_token", "").strip()
    try:
        claims = verify_access_token(token)
        membership = resolve_membership(claims["sub"], token)
    except Exception as exc:
        logger.warning("Supabase sign-in verification failed (%s).", type(exc).__name__)
        return JsonResponse({"error": "Supabase could not verify this sign-in. Please try again."}, status=401)
    if membership is None:
        return JsonResponse({"error": "This email does not have shop access. Ask your Admin to create an account."}, status=403)
    response = JsonResponse({"ok": True})
    max_age = max(1, min(3600, int(claims["exp"] - timezone.now().timestamp())))
    response.set_cookie("supabase_access_token", token, max_age=max_age, httponly=True, secure=not settings.DEBUG, samesite="Lax")
    return response


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        cache_key = f"health-check:{uuid.uuid4()}"
        cache.set(cache_key, "ok", timeout=5)
        cache_healthy = cache.get(cache_key) == "ok"
        cache.delete(cache_key)
        if not cache_healthy:
            raise RuntimeError("Cache health check failed")
    except Exception as exc:
        logger.warning("Health check failed (%s).", type(exc).__name__)
        return HttpResponse("unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


def dashboard(request):
    shop = request.membership.shop
    today = timezone.localdate()
    visible_sales = Sale.objects.filter(shop=shop, currency=shop.currency)
    if not request.membership.can("view_sales"):
        visible_sales = visible_sales.filter(cashier=request.membership)
    sales = visible_sales.filter(created_at__date=today)
    refunds = SaleReturn.objects.filter(sale__in=visible_sales, created_at__date=today)
    financials = calculate_financials(shop, today, today, sales=sales, returns=refunds, sale_scope=visible_sales)
    refund_total = financials["refund_total"]
    show_profit = request.membership.can_costs and request.membership.can_reports and request.membership.can("expenses")
    profit_today = financials["profit"] if show_profit else None
    profit_cost_coverage = financials["missing_cost_units"] if show_profit else None
    sales_trend = []
    trend_start = today - timedelta(days=6)
    period_sales = visible_sales.filter(created_at__date__gte=trend_start, created_at__date__lte=today)
    daily_financials = build_daily_financials(shop, trend_start, today, sales=period_sales, sale_scope=visible_sales)
    for day_offset in range(6, -1, -1):
        trend_date = today - timedelta(days=day_offset)
        day_financials = daily_financials.get(trend_date, {"net_sales": Decimal("0"), "profit": Decimal("0")})
        sales_trend.append({
            "label": trend_date.strftime("%a"),
            "date_label": trend_date.strftime("%d %b"),
            "net_sales": day_financials["net_sales"],
            "profit": day_financials["profit"] if show_profit else None,
        })
    max_trend_sales = max((max(Decimal("0"), day["net_sales"]) for day in sales_trend), default=Decimal("0"))
    for day in sales_trend:
        day["bar_percent"] = round(float(max(Decimal("0"), day["net_sales"]) / max_trend_sales * 100), 1) if max_trend_sales else 0

    sales_by_category = {}
    for row in SaleLine.objects.filter(sale__in=period_sales, sale__void_record__isnull=True).values("product__category").annotate(
        total=Sum("line_total") - Sum("tax_amount")
    ):
        category = row["product__category"] or "Uncategorized"
        sales_by_category[category] = sales_by_category.get(category, Decimal("0")) + (row["total"] or Decimal("0"))
    for row in ReturnLine.objects.filter(
        return_record__sale__in=period_sales,
        return_record__created_at__date__gte=trend_start,
        return_record__created_at__date__lte=today,
    ).values("sale_line__product__category").annotate(total=Sum("refund_amount") - Sum("tax_refund_amount")):
        category = row["sale_line__product__category"] or "Uncategorized"
        sales_by_category[category] = sales_by_category.get(category, Decimal("0")) - (row["total"] or Decimal("0"))
    for row in SaleLine.objects.filter(
        sale__in=period_sales,
        sale__void_record__created_at__date__gte=trend_start,
        sale__void_record__created_at__date__lte=today,
    ).values("product__category").annotate(total=Sum("line_total") - Sum("tax_amount")):
        category = row["product__category"] or "Uncategorized"
        sales_by_category[category] = sales_by_category.get(category, Decimal("0")) - (row["total"] or Decimal("0"))

    stock_by_category = {}
    for row in StockMovement.objects.filter(
        shop=shop, product__is_active=True, product__kind=Product.Kind.STOCK,
        product__tracking=Product.Tracking.QUANTITY,
    ).values("product__category").annotate(quantity=Sum("quantity_delta")):
        category = row["product__category"] or "Uncategorized"
        stock_by_category[category] = stock_by_category.get(category, 0) + (row["quantity"] or 0)
    for row in Device.objects.filter(
        shop=shop, status=Device.Status.IN_STOCK, product__is_active=True, product__kind=Product.Kind.STOCK,
    ).values("product__category").annotate(quantity=Count("id")):
        category = row["product__category"] or "Uncategorized"
        stock_by_category[category] = stock_by_category.get(category, 0) + row["quantity"]

    pie_colors = ["#338865", "#e3a338", "#4f83cc", "#a76bc1", "#df6d6d", "#36a6a6", "#7b8794", "#9b793d"]
    def make_pie(values):
        positive_values = [(label, value) for label, value in values.items() if value and value > 0]
        total = sum((value for _, value in positive_values), Decimal("0"))
        if not total:
            return {"slices": [], "style": "", "total": Decimal("0")}
        slices, stops, degrees = [], [], Decimal("0")
        for index, (label, value) in enumerate(sorted(positive_values, key=lambda pair: pair[1], reverse=True)):
            color = pie_colors[index % len(pie_colors)]
            next_degrees = degrees + value / total * Decimal("360")
            stops.append(f"{color} {degrees:.2f}deg {next_degrees:.2f}deg")
            slices.append({"label": label, "value": value, "percent": (value / total * 100).quantize(Decimal("0.1")), "color": color})
            degrees = next_degrees
        return {"slices": slices, "style": "conic-gradient(" + ", ".join(stops) + ")", "total": total}

    sales_pie = make_pie(sales_by_category)
    stock_pie = make_pie(stock_by_category)
    product_rows = Product.objects.filter(shop=shop, is_active=True, kind=Product.Kind.STOCK)
    quantity_by_product = {
        row["product_id"]: row["quantity"] or 0
        for row in StockMovement.objects.filter(
            shop=shop, product__tracking=Product.Tracking.QUANTITY,
        ).values("product_id").annotate(quantity=Sum("quantity_delta"))
    }
    serialized_by_product = {
        row["product_id"]: row["quantity"]
        for row in Device.objects.filter(
            shop=shop, status=Device.Status.IN_STOCK,
        ).values("product_id").annotate(quantity=Count("id"))
    }
    low_stock = []
    for product in product_rows:
        on_hand = serialized_by_product.get(product.pk, 0) if product.tracking == Product.Tracking.SERIALIZED else quantity_by_product.get(product.pk, 0)
        if on_hand <= product.reorder_level:
            low_stock.append({"name": product.name, "sku": product.sku, "on_hand": on_hand})
    context = {
        "sales_total": financials["net_sales"],
        "refund_total": refund_total,
        "sales_count": sales.filter(void_record__isnull=True).count(),
        "low_stock": low_stock[:8],
        "recent_sales": (Sale.objects.filter(shop=shop) if request.membership.can("view_sales") else Sale.objects.filter(shop=shop, cashier=request.membership)).select_related("cashier", "void_record").prefetch_related("lines")[:8],
        "product_count": product_rows.count(),
        "show_profit": show_profit,
        "profit_today": profit_today,
        "profit_cost_coverage": profit_cost_coverage,
        "sales_trend": sales_trend,
        "sales_pie": sales_pie,
        "stock_pie": stock_pie,
    }
    return render(request, "shop/dashboard.html", context)


def product_list(request):
    catalog = Product.objects.filter(shop=request.membership.shop, is_active=True)
    products = catalog
    query = request.GET.get("q", "").strip()
    if query:
        products = products.filter(name__icontains=query) | products.filter(sku__icontains=query) | products.filter(brand_model__icontains=query)
    suggestions = catalog.only("name", "brand_model", "sku").order_by("name")[:250]
    return render(request, "shop/products.html", {"products": products.distinct(), "query": query, "search_suggestions": suggestions})


def _product_form_context(form, product=None):
    return {
        "form": form,
        "product": product,
        "product_detail_fields": [form[name] for name in ("name", "sku", "category", "brand_model", "supplier")],
        "sale_setting_fields": [form[name] for name in ("kind", "is_second_hand", "price", "cost") if name in form.fields],
        "stock_fields": [form[name] for name in ("reorder_level", "opening_quantity") if name in form.fields],
    }


@permission_required("products_edit")
@require_http_methods(["GET", "POST"])
def product_create(request):
    form = ProductForm(request.POST if request.method == "POST" else None,
                       shop=request.membership.shop, can_view_costs=request.membership.can_costs)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            product = form.save(commit=False)
            product.shop = request.membership.shop
            product.save()
            opening_quantity = form.cleaned_data["opening_quantity"]
            if opening_quantity and product.kind == Product.Kind.STOCK and product.tracking == Product.Tracking.QUANTITY:
                StockMovement.objects.create(
                    shop=product.shop,
                    product=product,
                    quantity_delta=opening_quantity,
                    reason=StockMovement.Reason.OPENING,
                    note="Opening pieces recorded when product was added",
                    actor=request.membership,
                )
            record_event(product.shop, request.membership, "catalog.product_created", "product", product.pk, {"name": product.name, "sku": product.sku, "opening_pieces": opening_quantity})
        messages.success(request, "Product added.")
        return redirect("products")
    return render(request, "shop/product_form.html", _product_form_context(form))


@permission_required("products_edit")
@require_http_methods(["GET", "POST"])
def product_edit(request, product_id):
    shop = request.membership.shop
    with transaction.atomic():
        # Match checkout's lock order so editing cannot overwrite a concurrent sale.
        if request.method == "POST":
            Shop.objects.select_for_update().get(pk=shop.pk)
        products = Product.objects.filter(shop=shop, is_active=True)
        if request.method == "POST":
            products = products.select_for_update()
        product = get_object_or_404(products, pk=product_id)
        form = ProductForm(request.POST if request.method == "POST" else None,
                           instance=product, shop=shop, can_view_costs=request.membership.can_costs)
        if request.method == "POST" and form.is_valid():
            changed_fields = list(form.changed_data)
            try:
                with transaction.atomic():
                    # Write only editable fields, keeping stock and device records intact.
                    if changed_fields:
                        product.save(update_fields=changed_fields)
                        record_event(shop, request.membership, "catalog.product_updated", "product", product.pk,
                                     {"name": product.name, "sku": product.sku, "changed_fields": changed_fields})
            except IntegrityError:
                form.add_error("sku", "This SKU is already used by another item. Choose a different SKU.")
            else:
                messages.success(request, "Product updated.")
                return redirect("products")
    return render(request, "shop/product_form.html", _product_form_context(form, product))


@permission_required("products_edit")
@require_http_methods(["GET", "POST"])
def product_delete(request, product_id):
    shop = request.membership.shop
    if request.method == "POST":
        with transaction.atomic():
            Shop.objects.select_for_update().get(pk=shop.pk)
            product = get_object_or_404(Product.objects.select_for_update(), pk=product_id, shop=shop)
            if product.is_active:
                product.is_active = False
                product.save(update_fields=["is_active"])
                record_event(shop, request.membership, "catalog.product_deleted", "product", product.pk,
                             {"name": product.name, "sku": product.sku, "stock_on_hand": product.on_hand})
        messages.success(request, "Product deleted from inventory and checkout. Its history is preserved.")
        return redirect("products")
    product = get_object_or_404(Product, pk=product_id, shop=shop, is_active=True)
    return render(request, "shop/product_delete.html", {"product": product})


@permission_required("products_edit")
def product_import(request):
    if request.method == "POST" and request.POST.get("action") == "commit":
        token = request.POST.get("token", "")
        session_key = f"product_import:{token}"
        rows = request.session.get(session_key)
        if not rows or not token:
            messages.error(request, "This import preview expired. Upload the CSV again.")
            return redirect("product_import")
        shop = request.membership.shop
        if ImportBatch.objects.filter(token=token, shop=shop).exists():
            request.session.pop(session_key, None)
            messages.info(request, "This import was already completed.")
            return redirect("products")
        try:
            with transaction.atomic():
                batch = ImportBatch.objects.create(token=token, shop=shop, created_by=request.membership, rows_imported=len(rows))
                for row in rows:
                    if row["sku"] and Product.objects.filter(shop=shop, sku=row["sku"]).exists():
                        raise ValueError(f"SKU {row['sku']} was added after the preview. Upload the CSV again to review current data.")
                    for imei in row["imeis"]:
                        if Device.objects.filter(shop=shop, imei=imei).exists():
                            raise ValueError(f"IMEI/serial {imei} was added after the preview. Upload the CSV again to review current data.")
                    product = Product.objects.create(
                        shop=shop, name=row["name"], sku=row["sku"], category=row["category"], brand_model=row["brand_model"],
                        kind=row["kind"], tracking=row["tracking"], is_second_hand=row["is_second_hand"],
                        price=Decimal(row["price"]), cost=Decimal(row["cost"]) if row["cost"] else None, reorder_level=row["reorder_level"],
                    )
                    if product.tracking == Product.Tracking.SERIALIZED:
                        for imei in row["imeis"]:
                            device = Device.objects.create(
                                shop=shop, product=product, imei=imei, is_second_hand=product.is_second_hand,
                                condition=row["condition"] if product.is_second_hand else "",
                                condition_notes=row["condition_notes"], included_accessories=row["included_accessories"],
                                acquisition_source=row["acquisition_source"], acquisition_date=date.today() if product.is_second_hand else None,
                                acquisition_cost=Decimal(row["cost"]) if row["cost"] else None,
                            )
                            StockMovement.objects.create(shop=shop, product=product, device=device, quantity_delta=1, reason=StockMovement.Reason.OPENING, note="Opening stock import", actor=request.membership)
                    elif product.kind == Product.Kind.STOCK and row["opening_quantity"]:
                        StockMovement.objects.create(shop=shop, product=product, quantity_delta=row["opening_quantity"], reason=StockMovement.Reason.OPENING, note="Opening stock import", actor=request.membership)
                record_event(shop, request.membership, "inventory.csv_imported", "import_batch", batch.pk, {"rows_imported": len(rows)})
            del request.session[session_key]
            messages.success(request, f"Imported {len(rows)} products and their opening stock.")
            return redirect("products")
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("product_import")
        except IntegrityError:
            if ImportBatch.objects.filter(token=token, shop=shop).exists():
                request.session.pop(session_key, None)
                messages.info(request, "This import was already completed.")
                return redirect("products")
            messages.error(request, "The import conflicts with a product or IMEI added after preview. Upload the CSV again to review the latest stock.")
            return redirect("product_import")

    form = ProductImportForm(request.POST or None, request.FILES or None)
    preview_rows, errors, row_count, token = [], [], 0, ""
    if request.method == "POST" and request.POST.get("action") == "preview" and form.is_valid():
        preview_rows, errors, row_count = preview_product_csv(form.cleaned_data["csv_file"], request.membership.shop)
        if not errors and preview_rows:
            token = secrets.token_urlsafe(24)
            request.session[f"product_import:{token}"] = preview_rows
    return render(request, "shop/product_import.html", {"form": form, "preview_rows": preview_rows, "errors": errors, "row_count": row_count, "token": token})


@permission_required("products_edit")
def product_import_template(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="mobile-shop-products-template.csv"'
    writer = csv.writer(response)
    writer.writerow(["name", "sku", "category", "brand_model", "kind", "tracking", "is_second_hand", "price", "cost", "reorder_level", "opening_quantity", "imeis", "condition", "condition_notes", "included_accessories", "acquisition_source"])
    writer.writerow(["USB-C Cable", "ACC-001", "Accessories", "1m", "stock", "quantity", "false", "299.00", "100.00", "5", "12", "", "", "", "", ""])
    writer.writerow(["Example used phone", "USED-001", "Phones", "Brand Model 128GB", "stock", "serialized", "true", "12000.00", "9000.00", "0", "1", "111111111111111|222222222222222", "good", "Minor marks on frame", "Box and cable", "Customer buy-in"])
    writer.writerow(["Phone setup", "SVC-001", "Services", "", "service", "quantity", "false", "500.00", "", "0", "0", "", "", "", "", ""])
    return response


@permission_required("purchases")
def receive_stock(request):
    form = StockReceiveForm(request.POST or None, shop=request.membership.shop)
    if request.method == "POST" and form.is_valid():
        product = form.cleaned_data["product"]
        try:
            with transaction.atomic():
                purchase = Purchase.objects.create(shop=request.membership.shop, supplier=form.cleaned_data["supplier"], reference=form.cleaned_data["reference"], created_by=request.membership)
                if product.tracking == Product.Tracking.SERIALIZED:
                    for imei in form.cleaned_data["imei_list"]:
                        device = Device.objects.create(
                            shop=request.membership.shop, product=product, imei=imei,
                            is_second_hand=form.cleaned_data["is_second_hand"],
                            condition=form.cleaned_data["condition"] if form.cleaned_data["is_second_hand"] else "",
                            condition_notes=form.cleaned_data["condition_notes"] if form.cleaned_data["is_second_hand"] else "",
                            included_accessories=form.cleaned_data["included_accessories"] if form.cleaned_data["is_second_hand"] else "",
                            acquisition_source=form.cleaned_data["acquisition_source"] if form.cleaned_data["is_second_hand"] else "",
                            acquisition_date=date.today(), acquisition_cost=form.cleaned_data["unit_cost"],
                        )
                        StockMovement.objects.create(shop=request.membership.shop, product=product, device=device, quantity_delta=1, reason=StockMovement.Reason.PURCHASE, note=f"Received on {purchase.reference or 'purchase'}", purchase=purchase, actor=request.membership)
                else:
                    note = f"Received on {purchase.reference or 'purchase'}"
                    if product.is_second_hand or form.cleaned_data["is_second_hand"]:
                        note = f"Second-hand stock purchase · {form.cleaned_data['acquisition_source'] or purchase.reference or 'Cash purchase'}"
                    StockMovement.objects.create(shop=request.membership.shop, product=product, quantity_delta=form.cleaned_data["quantity"], reason=StockMovement.Reason.PURCHASE, note=note, purchase=purchase, actor=request.membership)
                if form.cleaned_data["unit_cost"] is not None:
                    product.cost = form.cleaned_data["unit_cost"]
                    product.save(update_fields=["cost"])
                record_event(request.membership.shop, request.membership, "purchasing.stock_received", "purchase", purchase.pk, {"supplier": purchase.supplier.name if purchase.supplier else "", "reference": purchase.reference, "product": product.name, "quantity": form.cleaned_data["quantity"], "second_hand": form.cleaned_data["is_second_hand"]})
            messages.success(request, f"Received {form.cleaned_data['quantity']} × {product.name} into stock.")
            return redirect("products")
        except Exception as exc:
            if "unique_shop_imei" in str(exc) or "UNIQUE constraint" in str(exc):
                form.add_error("imeis", "One of these IMEI or serial numbers is already recorded in this shop.")
            else:
                raise
    return render(request, "shop/receive_stock.html", {"form": form})


def point_of_sale(request):
    shop = request.membership.shop
    products = Product.objects.filter(shop=shop, is_active=True).order_by("name")
    stock_products = products.filter(kind=Product.Kind.STOCK)
    devices = Device.objects.filter(shop=shop, status=Device.Status.IN_STOCK).select_related("product").order_by("product__name", "imei")
    payload = []
    if request.method == "POST":
        try:
            idempotency_key = uuid.UUID(request.POST.get("request_key", ""))
            previous_sale = Sale.objects.filter(shop=shop, idempotency_key=idempotency_key).first()
            if previous_sale:
                return redirect("receipt", sale_id=previous_sale.pk)
            payload = json.loads(request.POST.get("cart_json", "[]"))
            tender = request.POST.get("tender", "cash").strip()[:32]
            if not isinstance(payload, list) or not payload:
                raise ValueError("Add at least one item and choose a payment method.")
            if tender not in {"cash", "upi", "card", "bank_transfer", "other"}:
                raise ValueError("Choose a valid payment method.")
            with transaction.atomic():
                # Serialize receipt numbering and checkout posting per shop.
                Shop.objects.select_for_update().get(pk=shop.pk)
                prepared = []
                price_changes = []
                reserved_quantities = {}
                reserved_devices = set()
                subtotal = Decimal("0")
                for raw in payload:
                    if not isinstance(raw, dict):
                        raise ValueError("Review the items in the current bill.")
                    product = get_object_or_404(Product.objects.select_for_update(), pk=int(raw["product_id"]), shop=shop, is_active=True)
                    quantity = int(raw.get("quantity", 1))
                    if quantity < 1 or product.kind == Product.Kind.SERVICE and quantity > 99:
                        raise ValueError("Enter a valid quantity.")
                    device = None
                    if product.kind == Product.Kind.STOCK and product.tracking == Product.Tracking.SERIALIZED:
                        if quantity != 1 or not raw.get("device_id"):
                            raise ValueError(f"Select an available IMEI for {product.name}.")
                        device = get_object_or_404(Device.objects.select_for_update(), pk=int(raw["device_id"]), shop=shop, product=product, status=Device.Status.IN_STOCK)
                        if device.pk in reserved_devices:
                            raise ValueError("The same IMEI cannot appear twice in a bill.")
                        reserved_devices.add(device.pk)
                    elif product.kind == Product.Kind.STOCK:
                        reserved_quantities[product.pk] = reserved_quantities.get(product.pk, 0) + quantity
                        if product.on_hand < reserved_quantities[product.pk]:
                            raise ValueError(f"Not enough stock for {product.name}; available: {product.on_hand}.")
                    unit_price = product.price
                    if "unit_price" in raw:
                        price_form = CheckoutLinePriceForm({"unit_price": raw["unit_price"]})
                        if not price_form.is_valid():
                            raise ValueError(f"Selling price for {product.name}: {price_form.errors['unit_price'][0]}")
                        unit_price = price_form.cleaned_data["unit_price"].quantize(CENT)
                        if unit_price != product.price:
                            if not request.membership.can("discounts"):
                                raise ValueError("You do not have permission to edit bill prices. Ask your Admin for access.")
                            price_changes.append({
                                "product_id": product.pk, "device_id": device.pk if device else None,
                                "quantity": quantity, "catalog_price": str(product.price),
                                "unit_price": str(unit_price),
                            })
                    amount = (unit_price * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
                    subtotal += amount
                    prepared.append((product, quantity, device, unit_price, amount))

                line_taxes = [(amount * shop.tax_rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP) for _, _, _, _, amount in prepared]
                tax = sum(line_taxes, Decimal("0"))
                total = (subtotal + tax).quantize(CENT)
                if any(value > MAX_MONEY for value in (subtotal, tax, total)):
                    raise ValueError("The bill amount is too large. Reduce the price or quantity.")
                receipt_number = f"{timezone.localdate():%y%m%d}-{Sale.objects.filter(shop=shop, created_at__date=timezone.localdate()).count() + 1:04d}"
                sale = Sale.objects.create(shop=shop, idempotency_key=idempotency_key, receipt_number=receipt_number, cashier=request.membership, shop_name_snapshot=shop.name, shop_address_snapshot=shop.address, shop_phone_snapshot=shop.phone, shop_email_snapshot=shop.email, currency=shop.currency, tax_rate_snapshot=shop.tax_rate, receipt_footer_snapshot=shop.receipt_footer, customer_name=request.POST.get("customer_name", "").strip()[:160], customer_phone=request.POST.get("customer_phone", "").strip()[:40], subtotal=subtotal, tax_total=tax, total=total, discount_total=Decimal("0"))
                for (product, quantity, device, unit_price, line_amount), line_tax in zip(prepared, line_taxes):
                    product_label = " ".join(value for value in (product.name, product.brand_model) if value)
                    device_suffix = f" · {device.imei} · {device.get_condition_display() if device.condition else 'New'}" if device else ""
                    description = product_label[:180 - len(device_suffix)] + device_suffix
                    SaleLine.objects.create(sale=sale, product=product, device=device, description=description, item_kind=product.kind, quantity=quantity, unit_price=unit_price, unit_cost=device.acquisition_cost if device else product.cost, tax_amount=line_tax, line_total=line_amount + line_tax)
                    if product.kind == Product.Kind.STOCK:
                        if device:
                            device.status = Device.Status.SOLD
                            device.save(update_fields=["status"])
                        StockMovement.objects.create(shop=shop, product=product, device=device, quantity_delta=-quantity, reason=StockMovement.Reason.SALE, note=f"Receipt {sale.receipt_number}", sale=sale, actor=request.membership)
                Payment.objects.create(sale=sale, tender=tender, amount=total, reference=request.POST.get("payment_reference", "").strip()[:100])
                record_event(shop, request.membership, "sales.completed", "sale", sale.pk, {"receipt": sale.receipt_number, "total": str(sale.total), "currency": sale.currency, "price_changes": price_changes})
            return redirect("receipt", sale_id=sale.pk)
        except IntegrityError:
            previous_sale = Sale.objects.filter(shop=shop, idempotency_key=request.POST.get("request_key", "")).first()
            if previous_sale:
                return redirect("receipt", sale_id=previous_sale.pk)
            raise
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            try:
                retry_key = uuid.UUID(request.POST.get("request_key", ""))
            except (ValueError, TypeError, AttributeError):
                retry_key = uuid.uuid4()
            return render(request, "shop/pos.html", {"products": products, "stock_products": stock_products, "devices": devices, "request_key": retry_key, "error": str(exc) or "Please review the sale details.", "cart_payload": payload if isinstance(payload, list) else [], "can_edit_prices": request.membership.can("discounts")})
    return render(request, "shop/pos.html", {"products": products, "stock_products": stock_products, "devices": devices, "request_key": uuid.uuid4(), "can_edit_prices": request.membership.can("discounts"), "cart_payload": []})


def receipt(request, sale_id):
    sale = get_object_or_404(Sale.objects.filter(shop=request.membership.shop).select_related("shop", "cashier", "void_record").prefetch_related("lines", "lines__device", "payments"), pk=sale_id)
    if not request.membership.can("view_sales") and sale.cashier_id != request.membership.pk:
        return HttpResponseForbidden("You can only view receipts for your own sales. Ask your Admin if you need wider access.")
    tender_labels = {"cash": "Cash", "upi": "UPI", "card": "Card", "bank_transfer": "Bank transfer", "other": "Other"}
    payment_methods = list(dict.fromkeys(
        tender_labels.get(payment.tender, payment.tender.replace("_", " ").title())
        for payment in sale.payments.all()
        if payment.kind == Payment.Kind.RECEIVED
    ))
    paid_total = sum((payment.amount for payment in sale.payments.all() if payment.kind == Payment.Kind.RECEIVED), Decimal("0"))
    return render(request, "shop/receipt.html", {
        "sale": sale,
        "has_returns": sale.returns.exists(),
        "payment_method": ", ".join(payment_methods),
        "paid_total": paid_total,
        "due_total": max(sale.total - paid_total, Decimal("0")),
    })


def sales(request):
    rows = Sale.objects.filter(shop=request.membership.shop)
    if not request.membership.can("view_sales"):
        rows = rows.filter(cashier=request.membership)
    rows = rows.select_related("cashier").prefetch_related("lines", "void_record").annotate(
        has_returns=Exists(SaleReturn.objects.filter(sale_id=OuterRef("pk")))
    )[:100]
    return render(request, "shop/sales.html", {"sales": rows})


@permission_required("voids")
def void_sale(request, sale_id):
    shop = request.membership.shop
    sale = get_object_or_404(Sale.objects.filter(shop=shop).select_related("cashier").prefetch_related("lines", "payments"), pk=sale_id)
    if not request.membership.can("view_sales") and sale.cashier_id != request.membership.pk:
        return HttpResponseForbidden("You can only void your own sales unless your Admin grants sales-history access.")
    existing_void = SaleVoid.objects.filter(sale=sale).first()
    if request.method == "POST" and not existing_void:
        reason = request.POST.get("reason", "").strip()[:240]
        try:
            if not reason:
                raise ValueError("A reason is required to void this sale.")
            with transaction.atomic():
                sale = Sale.objects.select_for_update().get(pk=sale.pk, shop=shop)
                existing_void = SaleVoid.objects.filter(sale=sale).first()
                if existing_void:
                    return redirect("receipt", sale_id=sale.pk)
                if SaleReturn.objects.filter(sale=sale).exists():
                    raise ValueError("This sale has a return or refund. It cannot be voided; process any remaining return against the receipt.")
                received = list(Payment.objects.filter(sale=sale, kind=Payment.Kind.RECEIVED))
                if sum((payment.amount for payment in received), Decimal("0")) != sale.total:
                    raise ValueError("The recorded payments do not match the sale total. Review the payment records before voiding.")
                void_record = SaleVoid.objects.create(sale=sale, created_by=request.membership, reason=reason, amount=sale.total)
                for line in sale.lines.select_related("product", "device"):
                    if line.item_kind != Product.Kind.STOCK or not line.product_id:
                        continue
                    if line.device_id:
                        device = Device.objects.select_for_update().get(pk=line.device_id, shop=shop)
                        if device.status != Device.Status.SOLD:
                            raise ValueError(f"IMEI {device.imei} is no longer marked sold. Review its inventory status before voiding.")
                        device.status = Device.Status.IN_STOCK
                        device.save(update_fields=["status"])
                    StockMovement.objects.create(shop=shop, product=line.product, device=line.device, quantity_delta=line.quantity, reason=StockMovement.Reason.VOID, note=f"Void receipt {sale.receipt_number}: {reason}", sale=sale, actor=request.membership)
                for payment in received:
                    Payment.objects.create(sale=sale, void_record=void_record, kind=Payment.Kind.VOID, tender=payment.tender, amount=payment.amount, reference=f"Void receipt {sale.receipt_number}")
                record_event(shop, request.membership, "sales.voided", "sale", sale.pk, {"receipt": sale.receipt_number, "amount": str(sale.total), "reason": reason})
            messages.success(request, f"Sale {sale.receipt_number} was voided. Stock and payment reversals are linked to the original receipt.")
            return redirect("receipt", sale_id=sale.pk)
        except ValueError as exc:
            messages.error(request, str(exc))
    return render(request, "shop/void_sale.html", {"sale": sale, "void_record": existing_void})


def _report_dates(request):
    today = timezone.localdate()
    default_start = (today - timedelta(days=29)).isoformat()
    start_raw = request.GET.get("start", default_start)
    end_raw = request.GET.get("end", today.isoformat())
    try:
        start_date = date.fromisoformat(start_raw)
        end_date = date.fromisoformat(end_raw)
        if start_date > end_date:
            raise ValueError
        return start_date, end_date, None
    except ValueError:
        return today, today, "Choose a valid date range. The report is showing today instead."


@permission_required("view_sales")
def reports(request):
    start_date, end_date, date_error = _report_dates(request)
    report = build_report(request.membership.shop, start_date, end_date)
    if date_error:
        messages.error(request, date_error)
    return render(request, "shop/reports.html", {
        "report": report,
        "start_date": start_date,
        "end_date": end_date,
        "can_view_costs": request.membership.can_costs,
        "can_view_profit": request.membership.can_costs and request.membership.can("expenses"),
    })


def reports_csv(request):
    if not request.membership.can("view_sales") or not request.membership.can("exports"):
        return HttpResponseForbidden("You need report and export access. Ask your Admin if you need access.")
    start_date, end_date, date_error = _report_dates(request)
    if date_error:
        return HttpResponseBadRequest(date_error)
    report = build_report(request.membership.shop, start_date, end_date)

    def safe_cell(value):
        value = "" if value is None else str(value)
        return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="shop-report-{start_date}-{end_date}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["Mobile Shop ERP report", safe_cell(request.membership.shop.name)])
    writer.writerow(["Date range", start_date.isoformat(), end_date.isoformat()])
    writer.writerow(["Currency", request.membership.shop.currency, "Timezone", request.membership.shop.timezone])
    writer.writerow(["Receipts created", report["sales_count"]])
    writer.writerow(["Gross sales before tax", report["gross_sales"]])
    writer.writerow(["Refunds", report["refund_total"]])
    writer.writerow(["Voided sales", report["void_total"]])
    writer.writerow(["Net sales after returns and voids, before tax", report["net_sales"]])
    writer.writerow(["Tax after refunds and voids", report["tax_total"]])
    if request.membership.can_costs:
        writer.writerow(["Cost of goods sold", report["cost_of_goods"]])
    if request.membership.can_costs and request.membership.can("expenses"):
        writer.writerow(["Expenses", report["expenses"]])
        writer.writerow(["Calculated profit", report["profit"] if report["profit"] is not None else "Incomplete: missing product costs"])
    writer.writerow([])
    columns = ["Product/service", "Type", "Net units", "Net sales before tax"]
    if request.membership.can_costs:
        columns += ["Cost of goods", "Gross margin"]
    writer.writerow(columns)
    for item in report["products"]:
        row = [safe_cell(item["name"]), item["kind"], f'{item["units"]} pcs', item["revenue"]]
        if request.membership.can_costs:
            row += [item["cost"], item["margin"]]
        writer.writerow(row)
    writer.writerow([])
    writer.writerow(["Tender", "Received", "Refunded / reversed", "Net"])
    for tender in report["tenders"]:
        writer.writerow([safe_cell(tender["name"]), tender["received"], tender["refunded"], tender["net"]])
    writer.writerow([])
    writer.writerow(["Low-stock item", "SKU", "On hand", "Reorder level"])
    for item in report["low_stock"]:
        writer.writerow([safe_cell(item["name"]), safe_cell(item["sku"]), f'{item["on_hand"]} pcs', f'{item["reorder_level"]} pcs'])
    return response


@permission_required("refunds")
def returns(request):
    shop = request.membership.shop
    sale = None
    query = request.GET.get("receipt", "").strip()
    visible_sales = Sale.objects.filter(shop=shop)
    if not request.membership.can("view_sales"):
        visible_sales = visible_sales.filter(cashier=request.membership)
    receipt_suggestions = visible_sales.order_by("-created_at").values_list("receipt_number", flat=True)[:100]
    if request.method == "POST":
        query = request.POST.get("receipt_number", "").strip()
        try:
            return_key = uuid.UUID(request.POST.get("request_key", ""))
        except (ValueError, TypeError):
            messages.error(request, "This return form has expired. Find the receipt again and retry.")
            return redirect("returns")
        existing_return = SaleReturn.objects.filter(pk=return_key, sale__shop=shop).first()
        if existing_return:
            return redirect("return_receipt", return_id=existing_return.pk)
        try:
            with transaction.atomic():
                sale = get_object_or_404(Sale.objects.select_for_update().filter(shop=shop).prefetch_related("lines", "lines__device"), receipt_number=query)
                if not request.membership.can("view_sales") and sale.cashier_id != request.membership.pk:
                    return HttpResponseForbidden("You can only process returns for your own sales unless your Admin grants sales-history access.")
                if SaleVoid.objects.filter(sale=sale).exists():
                    raise ValueError("This receipt has been voided and cannot be returned.")
                existing_return = SaleReturn.objects.filter(pk=return_key, sale__shop=shop).first()
                if existing_return:
                    return redirect("return_receipt", return_id=existing_return.pk)
                sale_line = get_object_or_404(SaleLine.objects.select_for_update(), pk=int(request.POST.get("line_id", "")), sale=sale)
                quantity = int(request.POST.get("quantity", "1"))
                reason = request.POST.get("reason", "").strip()[:240]
                tender = request.POST.get("refund_tender", "cash").strip()[:32]
                if not reason:
                    raise ValueError("Add a reason for the return.")
                if tender not in {"cash", "upi", "card", "bank_transfer", "other"}:
                    raise ValueError("Choose a valid refund payment method.")
                if quantity < 1:
                    raise ValueError("Return quantity must be at least one.")
                if sale_line.item_kind == Product.Kind.STOCK and sale_line.device_id and quantity != 1:
                    raise ValueError("A serialized phone must be returned one device at a time.")
                already_returned = sale_line.return_lines.aggregate(total=Sum("quantity"))["total"] or 0
                available_quantity = sale_line.quantity - already_returned
                if quantity > available_quantity:
                    raise ValueError(f"Only {available_quantity} item(s) from this sale are still eligible for return.")
                previous_lines = list(sale_line.return_lines.all())
                if quantity == available_quantity:
                    refund_amount = sale_line.line_total - sum((line.refund_amount for line in previous_lines), Decimal("0"))
                    tax_refund_amount = sale_line.tax_amount - sum((line.tax_refund_amount for line in previous_lines), Decimal("0"))
                else:
                    refund_amount = ((sale_line.line_total / Decimal(sale_line.quantity)) * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
                    tax_refund_amount = ((sale_line.tax_amount / Decimal(sale_line.quantity)) * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
                prior_refunds = sale.returns.aggregate(total=Sum("refund_total"))["total"] or Decimal("0")
                if prior_refunds + refund_amount > sale.total:
                    raise ValueError("Refund exceeds the remaining paid amount for this receipt.")

                if sale_line.item_kind == Product.Kind.SERVICE:
                    disposition = ReturnLine.Disposition.SERVICE
                    device = None
                else:
                    disposition = request.POST.get("disposition", ReturnLine.Disposition.INSPECTION)
                    if disposition not in {ReturnLine.Disposition.SELLABLE, ReturnLine.Disposition.INSPECTION, ReturnLine.Disposition.DAMAGED}:
                        raise ValueError("Choose a valid stock disposition.")
                    disposition_label = ReturnLine.Disposition(disposition).label
                    device = sale_line.device
                    if device and device.status != Device.Status.SOLD:
                        raise ValueError("This phone is no longer marked as sold, so it cannot be returned again.")

                return_record = SaleReturn.objects.create(id=return_key, sale=sale, created_by=request.membership, reason=reason, refund_total=refund_amount, refund_tender=tender)
                ReturnLine.objects.create(return_record=return_record, sale_line=sale_line, device=device, quantity=quantity, disposition=disposition, refund_amount=refund_amount, tax_refund_amount=tax_refund_amount)
                Payment.objects.create(sale=sale, return_record=return_record, kind=Payment.Kind.REFUND, tender=tender, amount=refund_amount, reference=f"Return {return_record.pk}")
                if sale_line.item_kind == Product.Kind.STOCK:
                    if device:
                        device.status = {
                            ReturnLine.Disposition.SELLABLE: Device.Status.IN_STOCK,
                            ReturnLine.Disposition.INSPECTION: Device.Status.INSPECTION,
                            ReturnLine.Disposition.DAMAGED: Device.Status.DAMAGED,
                        }[disposition]
                        device.save(update_fields=["status"])
                    delta = quantity if disposition == ReturnLine.Disposition.SELLABLE else 0
                    StockMovement.objects.create(shop=shop, product=sale_line.product, device=device, quantity_delta=delta, reason=StockMovement.Reason.RETURN, note=f"{disposition_label}: {reason}", sale=sale, actor=request.membership)
                record_event(shop, request.membership, "sales.returned", "sale_return", return_record.pk, {"receipt": sale.receipt_number, "amount": str(refund_amount), "reason": reason, "disposition": disposition})
            return redirect("return_receipt", return_id=return_record.pk)
        except (ValueError, TypeError, InvalidOperation) as exc:
            messages.error(request, str(exc) or "Review the return details and try again.")

    if query and sale is None:
        sale = Sale.objects.filter(shop=shop, receipt_number=query).prefetch_related("lines", "lines__device", "lines__return_lines").first()
        if sale is None:
            messages.error(request, "No receipt found with that number.")
        elif not request.membership.can("view_sales") and sale.cashier_id != request.membership.pk:
            return HttpResponseForbidden("You can only view returns for your own sales unless your Admin grants sales-history access.")
    eligible_lines = []
    is_voided = SaleVoid.objects.filter(sale=sale).exists() if sale else False
    if sale and not is_voided:
        for line in sale.lines.all():
            returned = line.return_lines.aggregate(total=Sum("quantity"))["total"] or 0
            remaining = line.quantity - returned
            if remaining:
                line.returnable_quantity = remaining
                line.return_key = uuid.uuid4()
                eligible_lines.append(line)
    return render(request, "shop/returns.html", {"sale": sale, "eligible_lines": eligible_lines, "query": query, "is_voided": is_voided, "receipt_suggestions": receipt_suggestions})


@permission_required("refunds")
def return_receipt(request, return_id):
    return_record = get_object_or_404(SaleReturn.objects.filter(sale__shop=request.membership.shop).select_related("sale", "sale__shop", "created_by").prefetch_related("lines", "lines__sale_line", "payments"), pk=return_id)
    if not request.membership.can("view_sales") and return_record.sale.cashier_id != request.membership.pk:
        return HttpResponseForbidden("You can only view returns for your own sales unless your Admin grants sales-history access.")
    refund_method = {"cash": "Cash", "upi": "UPI", "card": "Card", "bank_transfer": "Bank transfer", "other": "Other"}.get(
        return_record.refund_tender,
        return_record.refund_tender.replace("_", " ").title(),
    )
    return render(request, "shop/return_receipt.html", {"return_record": return_record, "refund_method": refund_method})


def shop_settings(request):
    if request.membership.role != Membership.Role.ADMIN:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Only an Admin can change shop settings.")
    shop = request.membership.shop
    form = ShopSettingsForm(request.POST or None, instance=shop)
    if request.method == "POST" and form.is_valid():
        previous = {"name": shop.name, "address": shop.address, "phone": shop.phone, "email": shop.email, "currency": shop.currency, "timezone": shop.timezone, "tax_rate": str(shop.tax_rate), "receipt_footer": shop.receipt_footer}
        form.save()
        current = {"name": shop.name, "address": shop.address, "phone": shop.phone, "email": shop.email, "currency": shop.currency, "timezone": shop.timezone, "tax_rate": str(shop.tax_rate), "receipt_footer": shop.receipt_footer}
        changed = {key: {"from": previous[key], "to": current[key]} for key in previous if previous[key] != current[key]}
        if changed:
            record_event(shop, request.membership, "settings.updated", "shop", shop.pk, {"changed_fields": changed})
        messages.success(request, "Shop settings saved. New sales will use the updated tax and receipt settings.")
        return redirect("shop_settings")
    return render(request, "shop/settings.html", {"form": form, "has_sales": Sale.objects.filter(shop=shop).exists()})


@permission_required("purchases")
def suppliers(request):
    shop = request.membership.shop
    form = SupplierForm(request.POST or None)
    if request.method == "POST":
        if request.POST.get("action") == "archive":
            supplier = get_object_or_404(Supplier, pk=request.POST.get("supplier_id"), shop=shop)
            supplier.is_active = False
            supplier.save(update_fields=["is_active"])
            record_event(shop, request.membership, "supplier.archived", "supplier", supplier.pk, {"name": supplier.name})
            messages.success(request, f"{supplier.name} was removed from the active supplier list. Purchase history is preserved.")
            return redirect("suppliers")
        if form.is_valid():
            supplier = form.save(commit=False)
            supplier.shop = shop
            supplier.save()
            record_event(shop, request.membership, "supplier.created", "supplier", supplier.pk, {"name": supplier.name})
            messages.success(request, f"Supplier {supplier.name} added.")
            return redirect("suppliers")
    supplier_products = Product.objects.filter(is_active=True).annotate(current_stock=Sum("movements__quantity_delta")).order_by("name")
    supplier_rows = Supplier.objects.filter(shop=shop).prefetch_related(Prefetch("products", queryset=supplier_products)).order_by("name")
    purchase_summaries = {
        row["supplier_id"]: row
        for row in Purchase.objects.filter(shop=shop, supplier__isnull=False)
        .values("supplier_id").annotate(count=Count("id"), last_received=Max("created_at"))
    }
    creation_dates = {
        row["target_id"]: row["created_on"]
        for row in AuditEvent.objects.filter(shop=shop, event_type="supplier.created", target_type="supplier")
        .order_by().values("target_id").annotate(created_on=Min("created_at"))
    }
    for supplier in supplier_rows:
        summary = purchase_summaries.get(supplier.pk, {})
        supplier.purchase_count = summary.get("count", 0)
        supplier.last_received = summary.get("last_received")
        supplier.created_on = creation_dates.get(str(supplier.pk))
    return render(request, "shop/suppliers.html", {"form": form, "suppliers": supplier_rows})


@permission_required("expenses")
def expenses(request):
    shop = request.membership.shop
    if request.method == "POST" and request.POST.get("action") == "reverse":
        reversal_form = ExpenseReversalForm(request.POST)
        if reversal_form.is_valid():
            with transaction.atomic():
                expense = get_object_or_404(Expense.objects.select_for_update().filter(shop=shop, reversal_of__isnull=True), pk=request.POST.get("expense_id"))
                if hasattr(expense, "reversal"):
                    messages.error(request, "This expense has already been corrected.")
                    return redirect("expenses")
                Expense.objects.create(
                    shop=shop,
                    category=expense.category,
                    description=f"Correction for expense #{expense.pk}: {expense.description}"[:200],
                    amount=expense.amount,
                    currency=expense.currency,
                    expense_date=expense.expense_date,
                    payment_method=expense.payment_method,
                    reference=expense.reference,
                    created_by=request.membership,
                    reversal_of=expense,
                    reversal_reason=reversal_form.cleaned_data["reason"],
                )
                record_event(shop, request.membership, "expenses.reversed", "expense", expense.pk, {"amount": str(expense.amount), "reason": reversal_form.cleaned_data["reason"]})
            messages.success(request, "Expense corrected. The original entry remains in the history.")
        else:
            messages.error(request, "Enter a reason for correcting this expense.")
        return redirect("expenses")

    form = ExpenseForm(request.POST or None)
    if request.method == "POST" and request.POST.get("action") != "reverse" and form.is_valid():
        with transaction.atomic():
            expense = form.save(commit=False)
            expense.shop = shop
            expense.created_by = request.membership
            expense.currency = shop.currency
            expense.save()
            record_event(shop, request.membership, "expenses.recorded", "expense", expense.pk, {"category": expense.category, "amount": str(expense.amount), "date": expense.expense_date.isoformat()})
        messages.success(request, "Expense saved.")
        return redirect("expenses")

    entries = Expense.objects.filter(shop=shop).select_related("created_by", "reversal", "reversal__created_by")[:100]
    total = Expense.objects.filter(shop=shop, currency=shop.currency, reversal_of__isnull=True, reversal__isnull=True).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    return render(request, "shop/expenses.html", {"form": form, "entries": entries, "total": total})


@permission_required("stock_adjust")
def stock_adjustments(request):
    shop = request.membership.shop
    quantity_form = QuantityStocktakeForm(shop=shop)
    if request.method == "POST":
        mode = request.POST.get("mode")
        if mode == "quantity":
            quantity_form = QuantityStocktakeForm(request.POST, shop=shop)
            if quantity_form.is_valid():
                product = quantity_form.cleaned_data["product"]
                counted = quantity_form.cleaned_data["counted_quantity"]
                reason = quantity_form.cleaned_data["reason"]
                reason_label = dict(QuantityStocktakeForm.REASONS)[reason]
                try:
                    with transaction.atomic():
                        product = Product.objects.select_for_update().get(pk=product.pk, shop=shop)
                        current = product.on_hand
                        delta = counted - current
                        if reason in {"damage", "loss"} and delta >= 0:
                            raise ValueError("For damage or loss, the physical count must be lower than the system count.")
                        if reason == "found" and delta <= 0:
                            raise ValueError("For found stock, the physical count must be higher than the system count.")
                        note = f"Stocktake ({reason_label}): system {current}, counted {counted}. {quantity_form.cleaned_data['note']}".strip()[:240]
                        StockMovement.objects.create(shop=shop, product=product, quantity_delta=delta, reason=StockMovement.Reason.ADJUSTMENT, note=note, actor=request.membership)
                        record_event(shop, request.membership, "inventory.stocktake", "product", product.pk, {"product": product.name, "before": current, "counted": counted, "delta": delta, "reason": reason_label})
                    messages.success(request, f"Stock count saved for {product.name}. Change: {delta:+d}.")
                    return redirect("stock_adjustments")
                except ValueError as exc:
                    quantity_form.add_error("counted_quantity", str(exc))
        else:
            messages.error(request, "Use the quantity stock-count form to record inventory corrections.")
    recent = StockMovement.objects.filter(shop=shop, reason=StockMovement.Reason.ADJUSTMENT, device__isnull=True).select_related("product", "actor").order_by("-created_at")[:30]
    return render(request, "shop/stock_adjustments.html", {"quantity_form": quantity_form, "recent": recent})


def sign_out(request):
    response = render(request, "shop/sign_out.html")
    response.delete_cookie("supabase_access_token")
    return response


def team(request):
    if request.membership.role != Membership.Role.ADMIN:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Only an Admin can manage staff access.")
    shop = request.membership.shop
    staff_form = StaffAccountForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create_staff":
            staff_form = StaffAccountForm(request.POST)
            if staff_form.is_valid():
                email = staff_form.cleaned_data["email"].strip().casefold()
                if Membership.objects.filter(shop=shop, email__iexact=email).exists():
                    staff_form.add_error("email", "This email already has shop access.")
                else:
                    try:
                        user_id = create_user(email, staff_form.cleaned_data["password"])
                        role = staff_form.cleaned_data["role"]
                        permissions = (
                            {key: True for key in staff_form.cleaned_data["permissions"]}
                            if role == Membership.Role.STAFF else {}
                        )
                        member = Membership.objects.create(
                            shop=shop,
                            supabase_user_id=user_id,
                            role=role,
                            email=email,
                            permissions=permissions,
                        )
                        record_event(shop, request.membership, "access.member_created", "membership", member.pk, {"email": email, "role": role, "permissions": permissions})
                        role_label = Membership.Role(role).label
                        messages.success(request, f"{role_label} account created for {email}. Share the temporary password with them directly.")
                        return redirect("team")
                    except SupabaseApiError as exc:
                        messages.error(request, str(exc))
                    except IntegrityError:
                        staff_form.add_error("email", "This account is already linked to shop access.")
        elif action == "permissions":
            member = get_object_or_404(Membership, pk=request.POST.get("member_id"), shop=shop, role=Membership.Role.STAFF)
            form = StaffPermissionsForm(request.POST)
            if form.is_valid():
                previous = dict(member.permissions)
                member.permissions = {key: True for key in form.cleaned_data["permissions"]}
                member.save(update_fields=["permissions"])
                record_event(shop, request.membership, "access.permissions_changed", "membership", member.pk, {"email": member.email, "from": previous, "to": member.permissions})
                messages.success(request, f"Permissions updated for {member.display_name or member.email}.")
                return redirect("team")
        elif action == "deactivate":
            member = get_object_or_404(Membership, pk=request.POST.get("member_id"), shop=shop, role=Membership.Role.STAFF)
            member.is_active = False
            member.save(update_fields=["is_active"])
            record_event(shop, request.membership, "access.staff_deactivated", "membership", member.pk, {"email": member.email})
            messages.success(request, f"{member.display_name or member.email} can no longer access this shop.")
            return redirect("team")
    memberships = Membership.objects.filter(shop=shop).order_by("role", "email")
    return render(request, "shop/team.html", {"staff_form": staff_form, "memberships": memberships, "permission_options": STAFF_PERMISSION_OPTIONS})
