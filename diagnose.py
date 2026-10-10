"""Read-only diagnostics. Default is local-only; --probe makes one search-page call."""

import argparse
import asyncio
import json

import httpx

import opensooq_client as oc
from market import COUNTRY, CURRENCY, LANGUAGE


async def diagnose(probe=False):
    result = {
        "country": COUNTRY,
        "currency": CURRENCY,
        "language": LANGUAGE,
        "probe_requested": probe,
        "status": "not_probed",
    }
    if probe:
        try:
            data = await oc.raw_search("iphone")
            result.update(status="available", sample_count=len(oc.extract_items(data)))
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            result.update(
                status="rate_limited" if code == 429 else "blocked" if code == 403 else "error",
                http_status=code,
            )
        except (httpx.HTTPError, oc.OpenSooqError) as error:
            result.update(status="error", error_type=type(error).__name__)
    result["requests"] = oc.request_status()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe", action="store_true", help="Run a paced, bounded live search probe."
    )
    result = asyncio.run(diagnose(parser.parse_args().probe))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"not_probed", "available"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
