import hashlib
import json
import time
import uuid
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import jwt
from django.core.cache import cache
from django.db import OperationalError
from django.test import RequestFactory, TestCase, override_settings
from redis.exceptions import ConnectionError as RedisConnectionError

from .models import AuditEvent, Membership, Payment, Product, Sale, Shop, StockMovement
from .services import auth, optional_cache
from .views import health


@override_settings(
    DEBUG=False, SECURE_SSL_REDIRECT=False,
    SUPABASE_URL="https://example.supabase.co", SUPABASE_ANON_KEY="public-test-key",
    SUPABASE_ISSUER="https://example.supabase.co/auth/v1",
    SUPABASE_JWKS_URL="https://example.supabase.co/auth/v1/.well-known/jwks.json",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ShopReliabilityTests(TestCase):
    def setUp(self):
        cache.clear()
        patcher = patch.object(optional_cache, "_retry_at", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.shop = Shop.objects.create(name="Reliability shop")
        self.member = Membership.objects.create(
            shop=self.shop, role="staff", supabase_user_id="verified-shop-user",
        )
        self.product = Product.objects.create(
            shop=self.shop, name="Shop cable", price="100.00", cost="40.00",
        )
        StockMovement.objects.create(
            shop=self.shop, product=self.product, quantity_delta=5,
            reason="opening", actor=self.member,
        )
        self.claims = {
            "sub": self.member.supabase_user_id, "exp": int(time.time()) + 300,
            "iss": "https://example.supabase.co/auth/v1", "aud": "authenticated",
            "session_id": str(uuid.uuid4()),
        }
        self.token = jwt.encode(self.claims, "test-signing-key-with-more-than-thirty-two-characters", algorithm="HS256")
        self.client.cookies["supabase_access_token"] = self.token
        self.provider = patch("shop.services.auth.get_authenticated_user", return_value={"id": self.member.supabase_user_id})
        self.provider_mock = self.provider.start()
        self.addCleanup(self.provider.stop)

    def assert_safe_outage(self, response, submission=False):
        self.assertContains(response, "The shop is temporarily unavailable", status_code=503)
        self.assertEqual(response["Retry-After"], "30")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotContains(response, self.token, status_code=503)
        self.assertNotContains(response, self.product.name, status_code=503)
        self.assertNotContains(response, "private-connection-details", status_code=503)
        if submission:
            self.assertContains(response, "check Sales for its receipt", status_code=503)
        self.assertNotContains(response, "http-equiv", status_code=503)

    def test_redis_read_outage_keeps_pages_and_checkout_working_with_fresh_identity_checks(self):
        with patch.object(optional_cache.cache, "get", side_effect=RedisConnectionError("private-connection-details")) as cache_get:
            with self.assertLogs("shop.services.optional_cache", level="WARNING") as logs:
                listing = self.client.get("/products/")
                self.assertContains(listing, self.product.name)
                self.assertNotContains(listing, "40.00")
                self.assertContains(self.client.get("/"), "Your shop, today")
                self.assertEqual(self.client.get("/products/new/").status_code, 403)
                payload = {
                    "request_key": str(uuid.uuid4()), "tender": "cash",
                    "cart_json": json.dumps([{"product_id": self.product.pk, "quantity": 1}]),
                }
                self.assertEqual(self.client.post("/sell/", payload).status_code, 302)
                self.assertEqual(self.client.post("/sell/", payload).status_code, 302)
            self.assertEqual(cache_get.call_count, 1)
        self.assertGreaterEqual(self.provider_mock.call_count, 5)
        self.assertEqual(AuditEvent.objects.filter(event_type="auth.session_started").count(), 1)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(self.product.on_hand, 4)
        self.assertNotIn("private-connection-details", " ".join(logs.output))
        self.assertNotIn(self.token, " ".join(logs.output))

    def test_redis_write_outage_does_not_reject_verified_sessions_or_duplicate_audits(self):
        with patch.object(optional_cache.cache, "set", side_effect=RedisConnectionError("private-connection-details")):
            self.assertEqual(self.client.get("/products/").status_code, 200)
            self.assertEqual(self.client.get("/products/").status_code, 200)
        self.assertEqual(self.provider_mock.call_count, 2)
        self.assertEqual(AuditEvent.objects.filter(event_type="auth.session_started").count(), 1)

    def test_cache_restart_does_not_duplicate_existing_session_audit(self):
        self.assertEqual(self.client.get("/products/").status_code, 200)
        cache.clear()
        self.assertEqual(self.client.get("/products/").status_code, 200)
        self.assertEqual(AuditEvent.objects.filter(event_type="auth.session_started").count(), 1)

    def test_expired_cached_token_cannot_access_shop(self):
        self.claims["exp"] = int(time.time()) - 1
        token = jwt.encode(self.claims, "test-signing-key-with-more-than-thirty-two-characters", algorithm="HS256")
        cache.set("supabase-session-claims:" + hashlib.sha256(token.encode()).hexdigest(), self.claims, timeout=30)
        self.client.cookies["supabase_access_token"] = token
        self.assertRedirects(self.client.get("/products/"), "/sign-in/", fetch_redirect_response=False)
        self.provider_mock.assert_not_called()
        self.assertFalse(AuditEvent.objects.exists())

    def test_cache_outage_never_substitutes_for_supabase_verification(self):
        self.provider_mock.side_effect = auth.SupabaseAuthUnavailable("private-connection-details")
        with patch.object(optional_cache.cache, "get", side_effect=RedisConnectionError()):
            self.assert_safe_outage(self.client.get("/products/"))
        self.assertFalse(AuditEvent.objects.exists())
        self.assertFalse(Sale.objects.exists())

    def test_wrong_supabase_identity_is_denied_during_cache_outage(self):
        self.provider_mock.return_value = {"id": "someone-else"}
        with patch.object(optional_cache.cache, "get", side_effect=RedisConnectionError()):
            self.assertRedirects(self.client.get("/products/"), "/sign-in/", fetch_redirect_response=False)
        self.assertFalse(AuditEvent.objects.exists())

    def test_wrong_issuer_audience_and_missing_expiry_remain_denied(self):
        for changes in ({"iss": "https://wrong.example/auth/v1"}, {"aud": "other"}, {"exp": None}):
            with self.subTest(changes=changes):
                claims = {**self.claims, **changes}
                if claims["exp"] is None:
                    del claims["exp"]
                self.client.cookies["supabase_access_token"] = jwt.encode(
                    claims, "test-signing-key-with-more-than-thirty-two-characters", algorithm="HS256",
                )
                self.assertRedirects(self.client.get("/products/"), "/sign-in/", fetch_redirect_response=False)
        self.provider_mock.assert_not_called()
        self.assertFalse(AuditEvent.objects.exists())

    def test_cache_recovers_after_connection_cooldown(self):
        with patch.object(optional_cache.cache, "get", side_effect=RedisConnectionError()) as get:
            self.assertIsNone(optional_cache.get("recovery-test"))
            self.assertIsNone(optional_cache.get("recovery-test"))
            self.assertEqual(get.call_count, 1)
        with patch.object(optional_cache, "monotonic", return_value=optional_cache._retry_at + 1):
            optional_cache.set("recovery-test", "recovered", timeout=30)
            self.assertEqual(optional_cache.get("recovery-test"), "recovered")

    def test_membership_database_outage_fails_closed_without_private_details(self):
        with patch("shop.middleware.resolve_membership", side_effect=OperationalError("private-connection-details")):
            with self.assertLogs("shop.middleware", level="WARNING") as logs:
                self.assert_safe_outage(self.client.get("/products/"))
        self.assertNotIn("private-connection-details", " ".join(logs.output))

    def test_view_database_outage_does_not_retry_a_submission(self):
        with patch("shop.views.Product.objects.select_for_update", side_effect=OperationalError("private-connection-details")):
            response = self.client.post(f"/stock/{self.product.pk}/quick/", {
                "action": "add", "quantity": 1, "request_key": str(uuid.uuid4()),
            })
        # Staff without purchase permission still receives the normal denial.
        self.assertEqual(response.status_code, 403)
        self.member.permissions = {"purchases": True}
        self.member.save(update_fields=["permissions"])
        with patch("shop.views.Product.objects.select_for_update", side_effect=OperationalError("private-connection-details")) as write:
            self.assert_safe_outage(self.client.post(f"/stock/{self.product.pk}/quick/", {
                "action": "add", "quantity": 1, "request_key": str(uuid.uuid4()),
            }), submission=True)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(self.product.on_hand, 5)

    def test_failed_audit_is_retried_after_database_recovery(self):
        with patch("shop.middleware.record_event", side_effect=OperationalError("private-connection-details")):
            self.assert_safe_outage(self.client.get("/products/"))
        self.assertFalse(AuditEvent.objects.exists())
        self.assertEqual(self.client.get("/products/").status_code, 200)
        self.assertEqual(AuditEvent.objects.filter(event_type="auth.session_started").count(), 1)

    def test_sign_in_reports_temporary_failure_without_setting_session_cookie(self):
        self.client.cookies.clear()
        self.provider_mock.side_effect = auth.SupabaseAuthUnavailable("private-connection-details")
        response = self.client.post("/auth/session/", {"access_token": self.token})
        self.assertEqual(response.status_code, 503)
        self.assertIn("temporarily unavailable", response.json()["error"])
        self.assertNotIn("supabase_access_token", response.cookies)

    def test_health_still_reports_a_real_cache_outage(self):
        with patch("shop.views.cache.set", side_effect=RedisConnectionError("private-connection-details")):
            with self.assertLogs("shop.views", level="WARNING") as logs:
                response = health(RequestFactory().get("/health/"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b"unavailable")
        self.assertIn("cache", " ".join(logs.output))
        self.assertNotIn("private-connection-details", " ".join(logs.output))

    def test_supabase_network_and_http_failures_are_distinguished_from_invalid_sessions(self):
        self.provider.stop()
        for error, expected in (
            (URLError("private-connection-details"), auth.SupabaseAuthUnavailable),
            (HTTPError("https://example.supabase.co", 503, "Unavailable", {}, None), auth.SupabaseAuthUnavailable),
            (HTTPError("https://example.supabase.co", 401, "Denied", {}, None), auth.SupabaseAuthError),
        ):
            with self.subTest(error=type(error).__name__), patch("shop.services.auth.urlopen", side_effect=error):
                with self.assertRaises(expected):
                    auth.get_authenticated_user(self.token)
