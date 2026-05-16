"""
email_validator.py — MX record validation for email addresses.
Results are cached per domain so bulk imports don't hammer DNS.
"""

import logging

logger = logging.getLogger("email_validator")

try:
    import dns.resolver
    _DNS_AVAILABLE = True
except ImportError:
    _DNS_AVAILABLE = False
    logger.warning("dnspython not installed — email MX validation disabled. Run: pip install dnspython")

# Domain-level cache: { "gmail.com": True, "baddomain.xyz": False }
_mx_cache: dict[str, bool] = {}


def check_mx(email: str) -> bool:
    """Return True if the email's domain has MX records, False otherwise.
    Returns True if dnspython is not installed (fail-open)."""
    if not _DNS_AVAILABLE:
        return True

    if not email or "@" not in email:
        return False

    domain = email.split("@")[-1].strip().lower()
    if domain in _mx_cache:
        return _mx_cache[domain]

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        result = len(answers) > 0
    except dns.resolver.NXDOMAIN:
        result = False  # Domain does not exist
    except dns.resolver.NoAnswer:
        result = False  # Domain exists but has no MX records
    except dns.resolver.NoNameservers:
        result = False  # No nameservers could be reached
    except Exception as exc:
        logger.debug("MX check failed for %s: %s", domain, exc)
        result = True   # Unknown error — fail-open, don't block the contact

    _mx_cache[domain] = result
    return result


def clear_cache():
    _mx_cache.clear()
