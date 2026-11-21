"""Shop-scoped editing and history-preserving deletion of saved entries."""
from django import forms
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .forms import ExpenseForm, SupplierForm, STAFF_PERMISSION_OPTIONS
from .models import AuditEvent, Device, Expense, Membership, Product, Purchase, Sale, Shop, StockMovement, Supplier
from .services.audit import record_event


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=200, label="Reason for this change")


class MemberForm(forms.Form):
    display_name = forms.CharField(max_length=160, required=False, label="Display name")
    permissions = forms.MultipleChoiceField(choices=STAFF_PERMISSION_OPTIONS, required=False, widget=forms.CheckboxSelectMultiple)


class SaleDetailsForm(forms.Form):
    customer_name = forms.CharField(max_length=160, required=False)
    customer_phone = forms.CharField(max_length=40, required=False)


class PurchaseDetailsForm(forms.Form):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), required=False)
    reference = forms.CharField(max_length=80, required=False)
    quantity = forms.IntegerField(min_value=1, label="Correct received quantity (pieces)")


class MovementForm(forms.Form):
    quantity_delta = forms.IntegerField(label="Correct stock change (+ to add, − to remove)")


CONFIG = {
    "supplier": (Supplier, "purchases", "suppliers"),
    "expense": (Expense, "expenses", "expenses"),
    "purchase": (Purchase, "purchases", "suppliers"),
    "sale": (Sale, "view_sales", "sales"),
    "member": (Membership, None, "team"),
    "movement": (StockMovement, "stock_adjust", "stock_adjustments"),
}


@require_http_methods(["GET", "POST"])
def manage_entry(request, kind, entry_id, action):
    if kind not in CONFIG or action not in {"edit", "delete"}:
        from django.http import Http404
        raise Http404
    model, permission, destination = CONFIG[kind]
    member = request.membership
    if not member or (kind == "member" and member.role != Membership.Role.ADMIN):
        return HttpResponseForbidden("Only an Admin can manage team members.")
    if kind == "sale":
        if not member.can("voids"):
            return HttpResponseForbidden("You need sale-correction permission.")
    elif permission and not member.can(permission):
        return HttpResponseForbidden("You do not have permission to change this entry.")
    shop = member.shop
    with transaction.atomic():
        # A consistent shop lock also serializes duplicate correction submissions.
        if request.method == "POST":
            Shop.objects.select_for_update().get(pk=shop.pk)
        entries = model.objects.filter(shop=shop)
        if request.method == "POST":
            entries = entries.select_for_update()
        entry = get_object_or_404(entries, pk=entry_id)
        if kind == "sale" and not member.can("view_sales") and entry.cashier_id != member.pk:
            return HttpResponseForbidden("You can only correct your own sales.")
        if kind == "member" and entry.role != Membership.Role.STAFF:
            return HttpResponseForbidden("These actions are for Staff accounts. Admin access is protected.")
        if kind == "movement" and (entry.reason != StockMovement.Reason.ADJUSTMENT or entry.device_id or entry.purchase_id or entry.sale_id):
            return HttpResponseForbidden("Correct this movement through its original sale or purchase.")
        reason_form = ReasonForm(request.POST if request.method == "POST" else None)
        form = None
        if action == "edit":
            data = request.POST if request.method == "POST" else None
            if kind == "supplier":
                form = SupplierForm(data, instance=entry)
            elif kind == "expense":
                form = ExpenseForm(data, initial={name: getattr(entry, name) for name in ExpenseForm._meta.fields})
            elif kind == "member":
                form = MemberForm(data, initial={"display_name": entry.display_name, "permissions": [key for key, enabled in entry.permissions.items() if enabled]})
            elif kind == "sale":
                form = SaleDetailsForm(data, initial={"customer_name": entry.customer_name, "customer_phone": entry.customer_phone})
            elif kind == "purchase":
                movements = list(StockMovement.objects.filter(purchase=entry, reason=StockMovement.Reason.PURCHASE).select_related("product"))
                form = PurchaseDetailsForm(data, initial={"supplier": entry.supplier_id, "reference": entry.reference, "quantity": sum(m.quantity_delta for m in movements)})
                from django.db.models import Q
                form.fields["supplier"].queryset = Supplier.objects.filter(shop=shop).filter(Q(is_active=True) | Q(pk=entry.supplier_id))
                if any(m.device_id for m in movements) or len({m.product_id for m in movements}) != 1:
                    form.fields["quantity"].disabled = True
                    form.fields["quantity"].help_text = "Serialized or multi-product receipts keep their original quantities."
            else:
                form = MovementForm(data, initial={"quantity_delta": entry.quantity_delta})
        before = {field: str(getattr(entry, field)) for field in (
            {"supplier": ["name", "phone", "email"], "member": ["display_name", "permissions"],
             "sale": ["customer_name", "customer_phone"], "purchase": ["supplier_id", "reference"]}.get(kind, [])
        )}
        if request.method == "POST" and reason_form.is_valid() and (form is None or form.is_valid()):
            reason = reason_form.cleaned_data["reason"]
            try:
                with transaction.atomic():
                    details = {"reason": reason, "before": before}
                    corrected = AuditEvent.objects.filter(shop=shop, target_type=kind, target_id=str(entry.pk), event_type__in=[f"{kind}.entry_edited", f"{kind}.entry_deleted"]).exists()
                    if kind in {"purchase", "movement"} and corrected:
                        raise ValueError("This entry already has a correction. Review the linked history before making another adjustment.")
                    if kind == "supplier":
                        if action == "edit":
                            entry = form.save()
                            details["after"] = {name: getattr(entry, name) for name in SupplierForm._meta.fields}
                        else:
                            entry.is_active = False
                            entry.save(update_fields=["is_active"])
                    elif kind == "member":
                        if action == "edit":
                            entry.display_name = form.cleaned_data["display_name"]
                            entry.permissions = {key: True for key in form.cleaned_data["permissions"]}
                            entry.save(update_fields=["display_name", "permissions"])
                            details["after"] = {"display_name": entry.display_name, "permissions": entry.permissions}
                        else:
                            entry.is_active = False
                            entry.save(update_fields=["is_active"])
                    elif kind == "expense":
                        if entry.reversal_of_id or hasattr(entry, "reversal"):
                            raise ValueError("This expense has already been corrected.")
                        reversal = Expense.objects.create(shop=shop, category=entry.category, description=f"Correction for expense #{entry.pk}", amount=entry.amount, currency=entry.currency, expense_date=entry.expense_date, payment_method=entry.payment_method, reference=entry.reference, created_by=member, reversal_of=entry, reversal_reason=reason)
                        details["reversal_id"] = reversal.pk
                        if action == "edit":
                            replacement = form.save(commit=False)
                            replacement.shop, replacement.created_by, replacement.currency = shop, member, entry.currency
                            replacement.save()
                            details["replacement_id"] = replacement.pk
                    elif kind == "sale":
                        if action == "delete":
                            return redirect("void_sale", sale_id=entry.pk)
                        if hasattr(entry, "void_record") or entry.returns.exists():
                            raise ValueError("Voided or returned receipts cannot be edited.")
                        for name, value in form.cleaned_data.items():
                            setattr(entry, name, value)
                        entry.save(update_fields=list(form.cleaned_data))
                        details["after"] = form.cleaned_data
                    elif kind == "purchase":
                        movements = list(StockMovement.objects.filter(purchase=entry, reason=StockMovement.Reason.PURCHASE).select_related("product"))
                        if not movements:
                            raise ValueError("No original received stock was found for this purchase.")
                        for product_id in sorted({m.product_id for m in movements}):
                            Product.objects.select_for_update().get(pk=product_id, shop=shop)
                        deltas = {}
                        for movement in movements:
                            deltas[movement.product_id] = deltas.get(movement.product_id, 0) - movement.quantity_delta
                        if action == "edit":
                            original_quantity = sum(m.quantity_delta for m in movements)
                            if not form.fields["quantity"].disabled:
                                deltas[movements[0].product_id] += form.cleaned_data["quantity"]
                            else:
                                deltas = {}
                            details["original_quantity"] = original_quantity
                            details["corrected_quantity"] = form.cleaned_data["quantity"]
                            entry.supplier = form.cleaned_data["supplier"]
                            entry.reference = form.cleaned_data["reference"]
                            entry.save(update_fields=["supplier", "reference"])
                            details["after"] = {"supplier_id": entry.supplier_id, "reference": entry.reference}
                        if action == "delete":
                            for movement in movements:
                                if movement.device_id:
                                    device = Device.objects.select_for_update().get(pk=movement.device_id, shop=shop)
                                    if device.status != Device.Status.IN_STOCK or StockMovement.objects.filter(device=device, pk__gt=movement.pk).exists():
                                        raise ValueError("A phone from this receipt has subsequent stock history. Correct that history first.")
                                    device.status = Device.Status.WRITTEN_OFF
                                    device.save(update_fields=["status"])
                                    StockMovement.objects.create(shop=shop, product=movement.product, device=device, purchase=entry, quantity_delta=-movement.quantity_delta, reason=StockMovement.Reason.ADJUSTMENT, note=f"Reverse purchase #{entry.pk}: {reason}"[:240], actor=member)
                        details["movement_ids"] = []
                        for product_id, delta in deltas.items():
                            product = Product.objects.get(pk=product_id, shop=shop)
                            if product.tracking == Product.Tracking.SERIALIZED:
                                continue
                            if product.on_hand + delta < 0:
                                raise ValueError("This correction would make stock negative. Review later sales or stock movements first.")
                            if delta:
                                correction = StockMovement.objects.create(shop=shop, product=product, purchase=entry, quantity_delta=delta, reason=StockMovement.Reason.ADJUSTMENT, note=f"Correct purchase #{entry.pk}: {reason}"[:240], actor=member)
                                details["movement_ids"].append(correction.pk)
                    else:
                        product = Product.objects.select_for_update().get(pk=entry.product_id, shop=shop)
                        desired = form.cleaned_data["quantity_delta"] if action == "edit" else 0
                        delta = desired - entry.quantity_delta
                        if product.on_hand + delta < 0:
                            raise ValueError("This correction would make stock negative.")
                        correction = StockMovement.objects.create(shop=shop, product=product, quantity_delta=delta, reason=StockMovement.Reason.ADJUSTMENT, note=f"Correct movement #{entry.pk}: {reason}"[:240], actor=member)
                        details["correction_id"] = correction.pk
                        details["original_delta"] = entry.quantity_delta
                        details["corrected_delta"] = desired
                    record_event(shop, member, f"{kind}.entry_{'edited' if action == 'edit' else 'deleted'}", kind, entry.pk, details)
                messages.success(request, "Entry updated. The change is recorded in history." if action == "edit" else "Entry removed or reversed. Its history is preserved.")
                return redirect(destination)
            except ValueError as exc:
                reason_form.add_error(None, str(exc))
    return render(request, "shop/manage_entry.html", {"entry": entry, "kind": kind, "action": action, "form": form, "reason_form": reason_form, "back_url": destination})
