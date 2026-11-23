"""Tests for subtrack.prefilter (synthetic fixtures only, no network)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from subtrack.gmail_client import parse_message
from subtrack.models import Email
from subtrack.prefilter import (
    KEEP_THRESHOLD,
    has_amount,
    is_otp_or_password_reset,
    score_email,
)


def make_email(
    subject: str = "",
    text: str = "",
    sender: str = "Service <billing@service.example>",
    list_unsubscribe: bool = False,
) -> Email:
    return Email(
        message_id="m_test",
        thread_id="t_test",
        sender=sender,
        subject=subject,
        received_at=datetime(2026, 3, 1, 9, 15, tzinfo=timezone.utc),
        list_unsubscribe=list_unsubscribe,
        text=text,
    )


class TestOtpAndPasswordReset:
    def test_otp_fixture_is_hard_dropped(self, gmail_message):
        email = parse_message(gmail_message("otp_email"))
        assert is_otp_or_password_reset(email) is True
        res = score_email(email)
        assert res.keep is False
        assert res.reasons == ["hard_drop:otp_or_password_reset"]

    def test_password_reset_fixture_is_hard_dropped(self, gmail_message):
        email = parse_message(gmail_message("password_reset"))
        assert is_otp_or_password_reset(email) is True
        res = score_email(email)
        assert res.keep is False
        assert res.reasons == ["hard_drop:otp_or_password_reset"]

    @pytest.mark.parametrize(
        "subject,body",
        [
            ("Your verification code", "Use 123456 to continue."),
            ("Security alert", "Your login code is 998877. Do not share this code."),
            ("Forgot your password?", "Click below to continue."),
            ("Action needed", "We received a request to reset your password for your account."),
        ],
    )
    def test_otp_and_reset_variants(self, subject, body):
        res = score_email(make_email(subject=subject, text=body))
        assert res.keep is False
        assert res.reasons == ["hard_drop:otp_or_password_reset"]

    def test_normal_receipt_with_footer_password_link_is_not_dropped(self):
        email = make_email(
            subject="Your monthly receipt from Cloudbox",
            text=(
                "Thank you for your payment of $9.99. Your monthly subscription renews on 1 April.\n"
                + ("Details line.\n" * 120)
                + "Trouble logging in? Forgot your password? Visit our help center."
            ),
        )
        assert is_otp_or_password_reset(email) is False
        assert score_email(email).keep is True


class TestHasAmount:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Charged $10.99 today", "$10.99"),
            ("Total: ₦4,500", "₦4,500"),
            ("Amount: USD 20.00", "USD 20.00"),
            ("Paid NGN 7,000.00", "NGN 7,000.00"),
            ("You paid 4,400 NGN", "4,400 NGN"),
            ("Debit of N1,000 on card", "N1,000"),
            ("Fee: N500", "N500"),
            ("Paid $20.00.", "$20.00"),
            ("You paid ₦4,500, thank you.", "₦4,500"),
            ("Costs £4.99 per month", "£4.99"),
            ("Received €9.99", "€9.99"),
        ],
    )
    def test_detects_billing_amounts(self, text, expected):
        assert has_amount(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "LIMITED TIME OFFER - $30 off Annual Subscription",
            "Get ₦500 cashback when you refer a friend",
            "Enjoy $10 credit on your first ride",
            "Offer of 5,000 shares at N3.20K each",
            "No currency here, just order 12345",
        ],
    )
    def test_ignores_discounts_and_non_amounts(self, text):
        assert has_amount(text) is None


class TestScoreEmail:
    def test_plain_receipt_fixture_kept(self, gmail_message):
        res = score_email(parse_message(gmail_message("plain_only")))
        assert res.keep is True
        assert res.score >= KEEP_THRESHOLD
        assert any(r.startswith("subject_billing:") for r in res.reasons)
        assert any(r.startswith("amount:") for r in res.reasons)

    def test_html_renewal_with_list_unsubscribe_still_kept(self, gmail_message):
        res = score_email(parse_message(gmail_message("short_plain_with_html")))
        assert res.keep is True
        assert "list_unsubscribe(-2)" in res.reasons
        assert any(r.startswith("amount:") for r in res.reasons)

    def test_payment_failed_with_list_unsubscribe_kept(self, gmail_message):
        res = score_email(parse_message(gmail_message("payment_failed")))
        assert res.keep is True
        assert any(r.startswith("urgent:payment_failed") for r in res.reasons)
        assert "list_unsubscribe(-2)" in res.reasons

    def test_trial_ending_kept(self, gmail_message):
        res = score_email(parse_message(gmail_message("zero_width")))
        assert res.keep is True
        assert any("trial" in r for r in res.reasons)

    def test_cancellation_with_list_unsubscribe_and_no_amount_kept(self):
        email = make_email(
            subject="You've canceled your MusicCo subscription",
            text="Your plan will remain active until the end of the billing period.",
            list_unsubscribe=True,
        )
        res = score_email(email)
        assert res.keep is True
        assert any("cancelled" in r for r in res.reasons)

    def test_price_change_with_list_unsubscribe_kept(self):
        email = make_email(
            subject="Changes to your Cloud Storage subscription",
            text="Starting next month, your plan price will change to $2.99/month.",
            list_unsubscribe=True,
        )
        res = score_email(email)
        assert res.keep is True
        assert any("price_change" in r for r in res.reasons)

    def test_promo_discount_fixture_skipped(self, gmail_message):
        res = score_email(parse_message(gmail_message("promo_discount")))
        assert res.keep is False
        assert any(r.startswith("marketing:") for r in res.reasons)
        assert not any(r.startswith("amount:") for r in res.reasons)

    def test_newsletter_article_fixture_skipped(self, gmail_message):
        res = score_email(parse_message(gmail_message("newsletter_article")))
        assert res.keep is False
        assert "list_unsubscribe(-2)" in res.reasons

    @pytest.mark.parametrize(
        "subject,body,unsub",
        [
            (
                "Confirm your subscription for Weekly Tech",
                "Click below to confirm your email subscription.",
                True,
            ),
            (
                "Choose a subscription plan for Byte News",
                "Upgrade to paid to read all posts.",
                True,
            ),
            (
                "Homeschooling: Parents on trial",
                "Read this week's newsletter update.",
                True,
            ),
            (
                "Software Engineer at Clinical Trial Systems - Apply Now",
                "12 new jobs matching your search.",
                True,
            ),
            (
                "FREE TRIAL | Deploy Now",
                "Start your free trial of the new game this weekend!",
                True,
            ),
            (
                "Reminder: Get Your Free Trial of Premium",
                "Hurry and claim your 7-day free trial today.",
                True,
            ),
            (
                "getting declined a lot? your resume might be hiding this",
                "Read our engineering newsletter for resume tips.",
                True,
            ),
            (
                "Your cloud storage is almost full",
                "You have used 85% of your free storage space.",
                False,
            ),
        ],
    )
    def test_common_non_billing_junk_skipped(self, subject, body, unsub):
        res = score_email(make_email(subject=subject, text=body, list_unsubscribe=unsub))
        assert res.keep is False, f"Expected SKIP for {subject!r}, got {res}"
