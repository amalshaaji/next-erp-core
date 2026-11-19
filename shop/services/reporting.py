from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Case, Count, DecimalField, ExpressionWrapper, F, IntegerField, Q, Sum, Value, When

from shop.models import Device, Expense, Payment, Product, ReturnLine, Sale, SaleLine, SaleReturn, SaleVoid, StockMovement

CENT = Decimal("0.01")


def build_daily_financials(shop, start_date, end_date, sales=None, sale_scope=None):
    """Aggregate chart metrics by day instead of running a full report per day."""
    if sales is None:
        sales = Sale.objects.filter(
            shop=shop, currency=shop.currency,
            created_at__date__gte=start_date, created_at__date__lte=end_date,
        )
    if sale_scope is not None:
        sales = sales.filter(pk__in=sale_scope.values("pk"))

    zero_money = Value(Decimal("0"), output_field=DecimalField(max_digits=18, decimal_places=2))
    revenue_expression = ExpressionWrapper(F("line_total") - F("tax_amount"), output_field=DecimalField(max_digits=18, decimal_places=2))
    cost_expression = ExpressionWrapper(F("unit_cost") * F("quantity"), output_field=DecimalField(max_digits=18, decimal_places=2))
    missing_expression = Case(
        When(unit_cost__isnull=True, item_kind=Product.Kind.STOCK, then=F("quantity")),
        default=Value(0), output_field=IntegerField(),
    )

    totals = defaultdict(lambda: {
        "sales": Decimal("0"), "returns": Decimal("0"), "voids": Decimal("0"),
        "cost": Decimal("0"), "expenses": Decimal("0"), "missing": 0,
    })
    for row in SaleLine.objects.filter(sale__in=sales).values("sale__created_at__date").annotate(
        revenue=Sum(revenue_expression),
        cost=Sum(Case(When(unit_cost__isnull=False, then=cost_expression), default=zero_money)),
        missing=Sum(missing_expression),
    ):
        day = row["sale__created_at__date"]
        totals[day]["sales"] += row["revenue"] or Decimal("0")
        totals[day]["cost"] += row["cost"] or Decimal("0")
        totals[day]["missing"] += row["missing"] or 0

    voids = SaleVoid.objects.filter(
        sale__shop=shop, sale__currency=shop.currency,
        created_at__date__gte=start_date, created_at__date__lte=end_date,
    )
    if sale_scope is not None:
        voids = voids.filter(sale__in=sale_scope)
    for row in SaleLine.objects.filter(sale__void_record__in=voids).values("sale__void_record__created_at__date").annotate(
        revenue=Sum(revenue_expression),
        cost=Sum(Case(When(unit_cost__isnull=False, then=cost_expression), default=zero_money)),
        missing=Sum(Case(
            When(sale__created_at__date=F("sale__void_record__created_at__date"), then=-missing_expression),
            default=missing_expression, output_field=IntegerField(),
        )),
    ):
        day = row["sale__void_record__created_at__date"]
        totals[day]["voids"] += row["revenue"] or Decimal("0")
        totals[day]["cost"] -= row["cost"] or Decimal("0")
        totals[day]["missing"] += row["missing"] or 0

    sellable_missing = Case(
        When(sale_line__unit_cost__isnull=True, sale_line__item_kind=Product.Kind.STOCK, then=F("quantity")),
        default=Value(0), output_field=IntegerField(),
    )
    return_lines = ReturnLine.objects.filter(
        return_record__sale__shop=shop,
        return_record__sale__currency=shop.currency,
        return_record__created_at__date__gte=start_date,
        return_record__created_at__date__lte=end_date,
    )
    if sale_scope is not None:
        return_lines = return_lines.filter(return_record__sale__in=sale_scope)
    for row in return_lines.values("return_record__created_at__date").annotate(
        refunds=Sum(ExpressionWrapper(F("refund_amount") - F("tax_refund_amount"), output_field=DecimalField(max_digits=18, decimal_places=2))),
        cost=Sum(Case(
            When(disposition=ReturnLine.Disposition.SELLABLE, sale_line__unit_cost__isnull=False,
                 then=ExpressionWrapper(F("sale_line__unit_cost") * F("quantity"), output_field=DecimalField(max_digits=18, decimal_places=2))),
            default=zero_money,
        )),
        missing=Sum(Case(
            When(disposition=ReturnLine.Disposition.SELLABLE,
                 sale_line__sale__created_at__date=F("return_record__created_at__date"), then=-sellable_missing),
            When(disposition=ReturnLine.Disposition.SELLABLE, then=sellable_missing),
            default=Value(0), output_field=IntegerField(),
        )),
    ):
        day = row["return_record__created_at__date"]
        totals[day]["returns"] += row["refunds"] or Decimal("0")
        totals[day]["cost"] -= row["cost"] or Decimal("0")
        totals[day]["missing"] += row["missing"] or 0

    for row in Expense.objects.filter(
        shop=shop, currency=shop.currency, expense_date__gte=start_date, expense_date__lte=end_date,
    ).values("expense_date").annotate(
        expenses=Sum(Case(When(reversal_of__isnull=True, then=F("amount")), default=-F("amount")))
    ):
        totals[row["expense_date"]]["expenses"] += row["expenses"] or Decimal("0")

    return {
        day: {
            "net_sales": (values["sales"] - values["returns"] - values["voids"]).quantize(CENT),
            "profit": (
                (values["sales"] - values["returns"] - values["voids"] - values["cost"] - values["expenses"]).quantize(CENT)
                if max(0, values["missing"]) == 0 else None
            ),
        }
        for day, values in totals.items()
    }


def calculate_financials(shop, start_date, end_date, sales=None, returns=None, sale_scope=None):
    """Calculate period sales, returns, cost of goods, expenses, and profit."""
    if sales is None:
        sales = Sale.objects.filter(
            shop=shop,
            currency=shop.currency,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        )
    if returns is None:
        returns = SaleReturn.objects.filter(
            sale__shop=shop,
            sale__currency=shop.currency,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        )

    sales_revenue = Decimal("0")
    sales_tax = Decimal("0")
    cost_of_goods = Decimal("0")
    missing_cost_units = 0
    sale_line_ids = set()
    for line in SaleLine.objects.filter(sale__in=sales).only(
        "id", "sale_id", "item_kind", "unit_price", "unit_cost", "tax_amount", "line_total", "quantity"
    ):
        sale_line_ids.add(line.pk)
        sales_revenue += line.line_total - line.tax_amount
        sales_tax += line.tax_amount
        if line.unit_cost is None:
            if line.item_kind != Product.Kind.SERVICE:
                missing_cost_units += line.quantity
        else:
            cost_of_goods += line.unit_cost * line.quantity

    voids = SaleVoid.objects.filter(
        sale__shop=shop, sale__currency=shop.currency, created_at__date__gte=start_date, created_at__date__lte=end_date
    )
    if sale_scope is not None:
        voids = voids.filter(sale__in=sale_scope)
    void_revenue = Decimal("0")
    void_tax = Decimal("0")
    for line in SaleLine.objects.filter(sale__void_record__in=voids).only(
        "id", "sale_id", "item_kind", "unit_cost", "tax_amount", "line_total", "quantity"
    ):
        void_revenue += line.line_total - line.tax_amount
        void_tax += line.tax_amount
        if line.unit_cost is None:
            if line.item_kind != Product.Kind.SERVICE:
                missing_cost_units += -line.quantity if line.pk in sale_line_ids else line.quantity
        else:
            cost_of_goods -= line.unit_cost * line.quantity

    refund_total = Decimal("0")
    refunds_before_tax = Decimal("0")
    refund_tax = Decimal("0")
    for line in ReturnLine.objects.filter(return_record__in=returns).select_related("sale_line", "sale_line__sale"):
        refund_total += line.refund_amount
        sale_line = line.sale_line
        tax_share = line.tax_refund_amount
        refund_tax += tax_share
        refunds_before_tax += line.refund_amount - tax_share
        if line.disposition == ReturnLine.Disposition.SELLABLE:
            if sale_line.unit_cost is None:
                if sale_line.item_kind != Product.Kind.SERVICE:
                    missing_cost_units += -line.quantity if sale_line.pk in sale_line_ids else line.quantity
            else:
                cost_of_goods -= sale_line.unit_cost * line.quantity

    void_total = voids.aggregate(total=Sum("amount"))["total"] or Decimal("0")

    expense_total = Decimal("0")
    for amount, reversal_of_id in Expense.objects.filter(
        shop=shop, currency=shop.currency, expense_date__gte=start_date, expense_date__lte=end_date
    ).values_list("amount", "reversal_of_id"):
        expense_total += -amount if reversal_of_id else amount

    missing_cost_units = max(0, missing_cost_units)
    net_sales = sales_revenue - refunds_before_tax - void_revenue
    profit = net_sales - cost_of_goods - expense_total if missing_cost_units == 0 else None
    return {
        "sales_revenue": sales_revenue.quantize(CENT),
        "sales_tax": (sales_tax - refund_tax - void_tax).quantize(CENT),
        "refund_total": refund_total.quantize(CENT),
        "refunds_before_tax": refunds_before_tax.quantize(CENT),
        "refund_tax": refund_tax.quantize(CENT),
        "void_total": void_total.quantize(CENT),
        "reversed_sales_before_tax": void_revenue.quantize(CENT),
        "net_sales": net_sales.quantize(CENT),
        "cost_of_goods": cost_of_goods.quantize(CENT),
        "expenses": expense_total.quantize(CENT),
        "profit": profit.quantize(CENT) if profit is not None else None,
        "missing_cost_units": missing_cost_units,
    }


def build_report(shop, start_date, end_date):
    sales = Sale.objects.filter(shop=shop, created_at__date__gte=start_date, created_at__date__lte=end_date)
    returns = SaleReturn.objects.filter(sale__shop=shop, sale__currency=shop.currency, created_at__date__gte=start_date, created_at__date__lte=end_date)
    sales = sales.filter(currency=shop.currency)
    financials = calculate_financials(shop, start_date, end_date, sales=sales, returns=returns)
    gross_sales = financials["sales_revenue"]
    tax_total = financials["sales_tax"]
    discounts = sales.aggregate(total=Sum("discount_total"))["total"] or Decimal("0")
    refund_total = financials["refund_total"]
    void_total = financials["void_total"]

    received_rows = Payment.objects.filter(sale__shop=shop, sale__currency=shop.currency, kind=Payment.Kind.RECEIVED, sale__created_at__date__gte=start_date, sale__created_at__date__lte=end_date).values("tender").annotate(total=Sum("amount"))
    refunded_rows = Payment.objects.filter(sale__shop=shop, sale__currency=shop.currency).filter(
        Q(kind=Payment.Kind.REFUND, return_record__created_at__date__gte=start_date, return_record__created_at__date__lte=end_date)
        | Q(kind=Payment.Kind.VOID, void_record__created_at__date__gte=start_date, void_record__created_at__date__lte=end_date)
    ).values("tender").annotate(total=Sum("amount"))
    tender_totals = defaultdict(lambda: {"received": Decimal("0"), "refunded": Decimal("0")})
    for row in received_rows:
        tender_totals[row["tender"]]["received"] += row["total"] or Decimal("0")
    for row in refunded_rows:
        tender_totals[row["tender"]]["refunded"] += row["total"] or Decimal("0")
    tenders = [{"name": name.replace("_", " ").title(), **values, "net": values["received"] - values["refunded"]} for name, values in sorted(tender_totals.items())]

    item_totals = defaultdict(lambda: {
        "name": "", "kind": Product.Kind.STOCK, "units": 0,
        "revenue": Decimal("0"), "cost": Decimal("0"), "missing_cost_units": 0,
    })
    def apply_item(key, units, revenue, cost, missing):
        product_id, description, kind = key
        item = item_totals[key]
        item.update(name=description, kind=kind)
        item["units"] += units or 0
        item["revenue"] += revenue or Decimal("0")
        item["cost"] += cost or Decimal("0")
        item["missing_cost_units"] += missing or 0

    money_field = DecimalField(max_digits=18, decimal_places=2)
    zero_money = Value(Decimal("0"), output_field=money_field)
    zero_units = Value(0, output_field=IntegerField())
    line_revenue = ExpressionWrapper(F("line_total") - F("tax_amount"), output_field=money_field)
    line_cost = ExpressionWrapper(F("unit_cost") * F("quantity"), output_field=money_field)
    missing_stock_units = Case(
        When(unit_cost__isnull=True, item_kind=Product.Kind.STOCK, then=F("quantity")),
        default=zero_units, output_field=IntegerField(),
    )

    for row in SaleLine.objects.filter(sale__in=sales).values("product_id", "description", "item_kind").annotate(
        units=Sum("quantity"), revenue=Sum(line_revenue),
        cost=Sum(Case(When(unit_cost__isnull=False, then=line_cost), default=zero_money)),
        missing=Sum(missing_stock_units),
    ):
        apply_item((row["product_id"], row["description"], row["item_kind"]), row["units"], row["revenue"], row["cost"], row["missing"])

    return_missing = Q(disposition=ReturnLine.Disposition.SELLABLE, sale_line__unit_cost__isnull=True, sale_line__item_kind=Product.Kind.STOCK)
    return_key = ("sale_line__product_id", "sale_line__description", "sale_line__item_kind")
    for row in ReturnLine.objects.filter(return_record__in=returns).values(*return_key).annotate(
        units=Sum("quantity"),
        revenue=Sum(ExpressionWrapper(F("refund_amount") - F("tax_refund_amount"), output_field=money_field)),
        cost=Sum(Case(
            When(disposition=ReturnLine.Disposition.SELLABLE, sale_line__unit_cost__isnull=False,
                 then=ExpressionWrapper(F("sale_line__unit_cost") * F("quantity"), output_field=money_field)),
            default=zero_money,
        )),
        missing_in_period=Sum(Case(When(return_missing & Q(sale_line__sale__in=sales), then=F("quantity")), default=zero_units, output_field=IntegerField())),
        missing_outside_period=Sum(Case(When(return_missing & ~Q(sale_line__sale__in=sales), then=F("quantity")), default=zero_units, output_field=IntegerField())),
    ):
        key = (row["sale_line__product_id"], row["sale_line__description"], row["sale_line__item_kind"])
        apply_item(key, -(row["units"] or 0), -(row["revenue"] or Decimal("0")), -(row["cost"] or Decimal("0")), (row["missing_outside_period"] or 0) - (row["missing_in_period"] or 0))

    voids = SaleVoid.objects.filter(
        sale__shop=shop, sale__currency=shop.currency,
        created_at__date__gte=start_date, created_at__date__lte=end_date,
    )
    for row in SaleLine.objects.filter(sale__void_record__in=voids).values("product_id", "description", "item_kind").annotate(
        units=Sum("quantity"), revenue=Sum(line_revenue),
        cost=Sum(Case(When(unit_cost__isnull=False, then=line_cost), default=zero_money)),
        missing_in_period=Sum(Case(When(Q(unit_cost__isnull=True, item_kind=Product.Kind.STOCK) & Q(sale__in=sales), then=F("quantity")), default=zero_units, output_field=IntegerField())),
        missing_outside_period=Sum(Case(When(Q(unit_cost__isnull=True, item_kind=Product.Kind.STOCK) & ~Q(sale__in=sales), then=F("quantity")), default=zero_units, output_field=IntegerField())),
    ):
        key = (row["product_id"], row["description"], row["item_kind"])
        apply_item(key, -(row["units"] or 0), -(row["revenue"] or Decimal("0")), -(row["cost"] or Decimal("0")), (row["missing_outside_period"] or 0) - (row["missing_in_period"] or 0))

    products = []
    for item in item_totals.values():
        item["missing_cost_units"] = max(0, item["missing_cost_units"])
        cost_known = item["missing_cost_units"] == 0
        margin = item["revenue"] - item["cost"] if cost_known else None
        products.append({
            "name": item["name"], "kind": item["kind"], "units": item["units"],
            "revenue": item["revenue"].quantize(CENT),
            "cost": item["cost"].quantize(CENT) if cost_known else None,
            "margin": margin.quantize(CENT) if margin is not None else None,
        })
    products.sort(key=lambda item: item["revenue"], reverse=True)

    quantity_stock = {row["product_id"]: row["quantity"] or 0 for row in StockMovement.objects.filter(shop=shop, product__tracking=Product.Tracking.QUANTITY).values("product_id").annotate(quantity=Sum("quantity_delta"))}
    serialized_stock = {row["product_id"]: row["quantity"] for row in Device.objects.filter(shop=shop, status=Device.Status.IN_STOCK).values("product_id").annotate(quantity=Count("id"))}
    low_stock = []
    for product in Product.objects.filter(shop=shop, is_active=True, kind=Product.Kind.STOCK).order_by("name"):
        available = serialized_stock.get(product.pk, 0) if product.tracking == Product.Tracking.SERIALIZED else quantity_stock.get(product.pk, 0)
        if available <= product.reorder_level:
            low_stock.append({"name": product.name, "sku": product.sku, "on_hand": available, "reorder_level": product.reorder_level})

    return {
        "sales_count": sales.count(),
        "gross_sales": gross_sales,
        "refund_total": refund_total,
        "refunds_before_tax": financials["refunds_before_tax"],
        "void_total": void_total,
        "net_sales": financials["net_sales"],
        "tax_total": tax_total,
        "discounts": discounts,
        "cost_of_goods": financials["cost_of_goods"],
        "expenses": financials["expenses"],
        "profit": financials["profit"],
        "missing_cost_units": financials["missing_cost_units"],
        "tenders": tenders,
        "products": products,
        "low_stock": low_stock,
    }
