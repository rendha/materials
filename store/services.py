"""Talks to the quotation website's read-only API (/api/materials/)."""
import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

from django.conf import settings


class QuotationApiError(Exception):
    pass


def _get(params, path=""):
    if not settings.QUOTATION_API_URL or not settings.QUOTATION_API_KEY:
        raise QuotationApiError("QUOTATION_API_URL and QUOTATION_API_KEY are not set on this website.")
    base = settings.QUOTATION_API_URL.rstrip("/") + "/" + (path.strip("/") + "/" if path else "")
    url = base + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"X-API-Key": settings.QUOTATION_API_KEY, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        if detail:
            raise QuotationApiError("The quotation website answered %s: %s" % (error.code, detail))
        raise QuotationApiError("The quotation website answered %s. Check the API key and address." % error.code)
    except Exception as error:                      # network down, sleeping server, bad JSON ...
        raise QuotationApiError("Could not reach the quotation website: %s" % error)


def search_quotations(query):
    return _get({"q": query}).get("quotations", [])


def estimate(specs):
    """The reverse of a quotation: the quotation website works out the material list (with ITS rates), the structure wage and the electrical
    work from the specifications only (panel brand / watts, inverter kW, phase, cable lengths ...)."""
    return _get({k: v for k, v in specs.items() if v not in ("", None)}, "estimate")


def get_quotation(quotation_id):
    return _get({"id": quotation_id})


def to_decimal(value, default=None):
    """'12', '12.5', '1,200' -> Decimal; anything else -> default."""
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return default
    return number if number.is_finite() else default
