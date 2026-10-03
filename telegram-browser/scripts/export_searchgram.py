import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def post(base_url, endpoint, payload):
    request = Request(
        base_url.rstrip("/") + endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=90) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    if not isinstance(result, dict):
        raise RuntimeError("Expected a JSON object from the API.")
    return result


def next_callback(page, query):
    current = page["page"]
    expected = f"search#{query}#{current + 1}"
    for button in page.get("navigation", []):
        if button.get("callback_data") == expected:
            return expected
    raise RuntimeError(
        f"Missing next-page callback for page {current}. "
        f"Navigation: {page.get('navigation')!r}"
    )


def export(args):
    seen_items = set()
    seen_pages = set()
    count = 0
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    # Exclusive creation prevents accidentally erasing an earlier export.
    with output.open("x", encoding="utf-8", buffering=1) as stream:
        page = post(args.url, "/api/search", {"query": args.query})
        expected_page = 1

        while True:
            if page.get("query") != args.query:
                raise RuntimeError("Response query does not match the request.")

            if page.get("total_results") == 0 and not page.get("items"):
                print("No results.")
                break

            number = page.get("page")
            total_pages = page.get("total_pages")
            items = page.get("items")

            if (
                type(number) is not int
                or type(total_pages) is not int
                or not 1 <= number <= total_pages
                or not isinstance(items, list)
            ):
                raise RuntimeError("Invalid page metadata.")

            if number != expected_page:
                raise RuntimeError(
                    f"Expected page {expected_page}, received {number}. "
                    "Stopping to avoid exporting stale results."
                )

            signature = tuple(item["callback_data"] for item in items)
            if signature in seen_pages:
                raise RuntimeError("Repeated page contents; stopping.")
            seen_pages.add(signature)

            if not items:
                raise RuntimeError("Unexpected empty page; stopping.")

            for item in items:
                key = item["callback_data"]
                if key in seen_items:
                    continue
                if not key.startswith("dl_"):
                    raise RuntimeError(f"Unexpected result identifier: {key}")

                record = {
                    "query": args.query,
                    "page": number,
                    "search_message_id": page["message_id"],
                    "result_title": item["title"],
                    "size": item.get("size"),
                    "callback_data": key,
                }
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                seen_items.add(key)
                count += 1

                if count >= args.max_results:
                    break

            print(
                f"Page {number}/{total_pages} | saved {count} unique results",
                flush=True,
            )

            if count >= args.max_results:
                print("Reached requested result limit.")
                break
            if number == total_pages:
                print("Reached final page.")
                break

            callback = next_callback(page, args.query)
            time.sleep(args.delay)
            page = post(
                args.url,
                "/api/search/navigate",
                {
                    "message_id": page["message_id"],
                    "callback_data": callback,
                    "query": args.query,
                },
            )
            expected_page = number + 1

    print(f"Saved {count} results to {output.resolve()}")


def main():
    parser = argparse.ArgumentParser(
        description="Export SearchGram result titles as JSON Lines."
    )
    parser.add_argument("query")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-results", type=int, default=1000)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--url", default="http://127.0.0.1:8788")
    args = parser.parse_args()
    args.query = args.query.strip()

    if not args.query or args.max_results < 1 or args.delay < 1:
        parser.error("Use a nonempty query, positive limit, and delay >= 1.")

    try:
        export(args)
    except FileExistsError:
        print("Output already exists; choose a new filename.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Already-written results are preserved.")
        return 130
    except Exception as exc:
        print(
            f"Export stopped: {exc}\nAlready-written results are preserved.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
