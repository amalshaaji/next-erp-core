import csv
import io
from decimal import Decimal, InvalidOperation

from shop.models import Device, Product

REQUIRED_COLUMNS = {
    "name", "sku", "category", "brand_model", "kind", "tracking", "is_second_hand",
    "price", "cost", "reorder_level", "opening_quantity", "imeis", "condition",
    "condition_notes", "included_accessories", "acquisition_source",
}
CONDITIONS = {choice for choice, _ in Device.Condition.choices}


def _fits_money_field(value):
    if not value.is_finite():
        return False
    parts = value.as_tuple()
    fractional_digits = max(0, -parts.exponent)
    integer_digits = max(0, value.adjusted() + 1) if value else 0
    return fractional_digits <= 2 and integer_digits <= 10


def preview_product_csv(upload, shop):
    try:
        content = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], ["The file must use UTF-8 text encoding."], 0
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return [], ["The file is empty or does not have a header row."], 0
    missing = sorted(REQUIRED_COLUMNS - {name.strip() for name in reader.fieldnames if name})
    if missing:
        return [], ["Missing columns: " + ", ".join(missing)], 0

    source_rows = list(reader)
    if len(source_rows) > 5000:
        return [], ["Import at most 5,000 product rows per file."], len(source_rows)
    rows = []
    errors = []
    seen_skus = set()
    seen_imeis = set()
    existing_skus = set(Product.objects.filter(shop=shop).exclude(sku="").values_list("sku", flat=True))
    existing_imeis = set(Device.objects.filter(shop=shop).values_list("imei", flat=True))
    for row_number, raw in enumerate(source_rows, start=2):
        row = {str(key).strip(): (value or "").strip() for key, value in raw.items() if key}
        row_errors = []
        name = row.get("name", "")
        sku = row.get("sku", "")
        kind = row.get("kind", "stock").lower() or "stock"
        tracking = row.get("tracking", "quantity").lower() or "quantity"
        second_hand_raw = row.get("is_second_hand", "false").lower() or "false"
        second_hand = second_hand_raw in {"true", "yes", "1"}
        condition = row.get("condition", "").lower()
        try:
            price = Decimal(row.get("price", ""))
            if price < 0 or not _fits_money_field(price):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            price = Decimal("0")
            row_errors.append("price must be a non-negative amount with at most 10 digits before and 2 digits after the decimal")
        try:
            cost = Decimal(row["cost"]) if row.get("cost") else None
            if cost is not None and (cost < 0 or not _fits_money_field(cost)):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            cost = None
            row_errors.append("cost must be blank or a non-negative amount with at most 10 digits before and 2 digits after the decimal")
        try:
            reorder = int(row.get("reorder_level") or 0)
            opening_quantity = int(row.get("opening_quantity") or 0)
            if reorder < 0 or opening_quantity < 0:
                raise ValueError
        except ValueError:
            reorder = opening_quantity = 0
            row_errors.append("reorder_level and opening_quantity must be zero or greater whole numbers")
        imeis = [value.strip() for value in row.get("imeis", "").split("|") if value.strip()]

        if not name:
            row_errors.append("name is required")
        if len(sku) > 80:
            row_errors.append("sku must be 80 characters or fewer")
        if second_hand_raw not in {"true", "false", "yes", "no", "1", "0"}:
            row_errors.append("is_second_hand must be true or false")
        if len(row.get("category", "")) > 100:
            row_errors.append("category must be 100 characters or fewer")
        if len(row.get("included_accessories", "")) > 240 or len(row.get("acquisition_source", "")) > 120:
            row_errors.append("included_accessories must be 240 characters or fewer and acquisition_source 120 characters or fewer")
        if kind not in {Product.Kind.STOCK, Product.Kind.SERVICE}:
            row_errors.append("kind must be stock or service")
        if tracking not in {Product.Tracking.QUANTITY, Product.Tracking.SERIALIZED}:
            row_errors.append("tracking must be quantity or serialized")
        if kind == Product.Kind.SERVICE and (opening_quantity or imeis or second_hand):
            row_errors.append("service rows cannot have stock, IMEIs, or second-hand status")
        if second_hand and tracking != Product.Tracking.SERIALIZED:
            row_errors.append("second-hand products must use serialized tracking")
        if tracking == Product.Tracking.SERIALIZED and opening_quantity not in {0, len(imeis)}:
            row_errors.append("for serialized products, opening_quantity must be blank/zero or equal the number of IMEIs")
        if tracking == Product.Tracking.SERIALIZED and opening_quantity and not imeis:
            row_errors.append("serialized opening stock requires one IMEI per phone in the imeis column, separated by |")
        if tracking == Product.Tracking.QUANTITY and imeis:
            row_errors.append("quantity-tracked products must leave imeis blank")
        if second_hand and imeis and condition not in CONDITIONS:
            row_errors.append("second-hand serialized devices need condition like_new, good, fair, or parts")
        if sku and sku in existing_skus | seen_skus:
            row_errors.append("sku already exists in this shop or appears more than once in the file")
        seen_skus.add(sku) if sku else None
        for imei in imeis:
            if len(imei) > 32:
                row_errors.append(f"IMEI/serial {imei} is longer than 32 characters")
            if imei in existing_imeis or imei in seen_imeis:
                row_errors.append(f"IMEI/serial {imei} already exists in this shop or appears more than once in the file")
            seen_imeis.add(imei)
        if row_errors:
            errors.append(f"Row {row_number}: " + "; ".join(row_errors))
        else:
            rows.append({
                "row_number": row_number, "name": name, "sku": sku, "category": row.get("category", ""),
                "brand_model": row.get("brand_model", ""), "kind": kind, "tracking": tracking,
                "is_second_hand": second_hand, "price": str(price), "cost": str(cost) if cost is not None else "",
                "reorder_level": reorder, "opening_quantity": opening_quantity, "imeis": imeis,
                "condition": condition, "condition_notes": row.get("condition_notes", ""),
                "included_accessories": row.get("included_accessories", ""),
                "acquisition_source": row.get("acquisition_source", ""),
            })
    if not rows and not errors:
        errors.append("No product rows were found.")
    return rows, errors, len(rows) + len(errors)
