from shop.models import AuditEvent


def record_event(shop, actor, event_type, target_type="", target_id="", details=None):
    """Append a shop audit event. Call inside the operation's transaction when possible."""
    return AuditEvent.objects.create(
        shop=shop,
        actor=actor,
        event_type=event_type,
        target_type=target_type,
        target_id=str(target_id or ""),
        details=details or {},
    )
