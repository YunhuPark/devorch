"""Credential shapes that were missed (agent test report §7, 2026-11-24) — and the ones deliberately NOT blocked.

Blocked (diff is not sent to an external reviewer; file is left out of reviewer context): Slack ``xox?-``, Stripe LIVE ``sk_live_``/``rk_live_``,
GitHub fine-grained ``github_pat_``. Shown/logged text also masks JWTs and the password inside ``scheme://user:pass@host``, but those do NOT block a
diff: they are common in ordinary code and fixtures, and blocking would stop legitimate work. Fake values are assembled at run time, so this file
itself contains no credential-shaped literal.
"""

import pytest

from devorch.security.secret_redactor import REDACTED, has_secret_literal, redact

SLACK = "xox" + "b-" + "1234567890-" + "abcdefABCDEF"
SLACK_USER = "xox" + "p-" + "1" * 12
STRIPE_LIVE = "sk" + "_live_" + "a1B2" * 6
STRIPE_RESTRICTED = "rk" + "_live_" + "Z9y8" * 6
GH_FINE = "github" + "_pat_" + "11ABCDEFG0" + "x" * 40
JWT = "ey" + "J" + "hbGciOiJIUzI1NiJ9" + ".ey" + "J" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


@pytest.mark.parametrize("token", [SLACK, SLACK_USER, STRIPE_LIVE, STRIPE_RESTRICTED, GH_FINE])
def test_newly_covered_tokens_are_detected_and_masked(token):
    line = f'CLIENT = make_client("{token}")'
    assert has_secret_literal(line)
    assert token not in redact(line) and REDACTED in redact(line)


def test_stripe_test_keys_are_not_a_reason_to_block():
    assert not has_secret_literal("sk" + "_test_" + "a1B2" * 6)


def test_a_jwt_is_masked_in_output_but_does_not_block_a_diff():
    line = f"EXAMPLE = '{JWT}'"
    assert not has_secret_literal(line)
    assert JWT not in redact(line) and REDACTED in redact(line)


@pytest.mark.parametrize(
    "url, secret, kept",
    [("postgres://app:" + "s3cr3t" + "@db:5432/shop", "s3cr3t", ["postgres://app:", "@db:5432/shop"]),
     ("redis://default:" + "pa55" + "@cache:6379", "pa55", ["redis://default:", "@cache:6379"])],
)
def test_only_the_password_inside_a_url_is_masked_and_it_does_not_block(url, secret, kept):
    out = redact(f'DATABASE_URL = "{url}"')
    assert secret not in out and all(k in out for k in kept)
    assert not has_secret_literal(url)


def test_urls_without_a_password_are_left_alone():
    for url in ["https://example.com/path", "postgres://localhost/db", "https://user@example.com/x", "git@github.com:org/repo.git"]:
        assert redact(url) == url


def test_existing_patterns_still_work():
    assert has_secret_literal("k = '" + "sk-" + "x" * 30 + "'")
    assert has_secret_literal("AK" + "IA" + "ABCDEFGHIJKLMNOP")
