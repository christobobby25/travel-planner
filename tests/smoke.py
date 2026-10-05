"""Smoke test for the MCP servers and API keys. No LLM or AWS needed.

Run from the project root:  python -m tests.smoke

For each server: connect, check the tools the agent gets match its allowlist, and make one
real call. Exits non-zero if anything fails.
"""
import asyncio
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv(".env")

from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402

from agents.flight_agent import duffel_client  # noqa: E402
from agents.hotel_graph import LITEAPI_ALLOWED_TOOLS, filter_hotel_tools, hotel_mcp_config  # noqa: E402
from agents.weather_agent import ACCUWEATHER_ALLOWED_TOOLS, accuweather_client  # noqa: E402


def preview(content, n=150) -> str:
    text = " ".join(c.get("text", "") for c in content) if isinstance(content, list) else str(content)
    return text[:n].replace("\n", " ") + ("…" if len(text) > n else "")


def check_tool_names(got: set, expected: set) -> None:
    missing = expected - got
    if missing:
        raise AssertionError(f"allowlisted tools not offered by server (renamed?): {sorted(missing)}")
    extra = got - expected
    if extra:
        raise AssertionError(f"agent gets tools outside the allowlist: {sorted(extra)}")


def check_strands_server(client, expected: set, tool: str, args: dict) -> str:
    with client:
        names = {t.tool_name for t in client.list_tools_sync()}
        check_tool_names(names, expected)
        result = client.call_tool_sync("smoke", tool, args)
    if result["status"] != "success":
        raise AssertionError(f"{tool} failed: {preview(result['content'], 400)}")
    return f"{len(names)} tools, {tool} -> {preview(result['content'])}"


async def check_hotels() -> str:
    tools = filter_hotel_tools(await MultiServerMCPClient({"hotels": hotel_mcp_config()}).get_tools())
    check_tool_names({t.name for t in tools}, LITEAPI_ALLOWED_TOOLS)
    search = next(t for t in tools if t.name == "get_data_hotels")
    out = await search.ainvoke({"countryCode": "US", "cityName": "Chicago", "limit": 1})
    return f"{len(tools)} tools, get_data_hotels -> {preview(out)}"


CHECKS = {
    "duffel": lambda: check_strands_server(
        duffel_client,
        {"search_flights", "get_offer"},
        "search_flights",
        {"origin": "DFW", "destination": "ORD",
         "departure_date": (date.today() + timedelta(days=30)).isoformat(), "max_results": 1},
    ),
    "accuweather": lambda: check_strands_server(
        accuweather_client,
        set(ACCUWEATHER_ALLOWED_TOOLS),
        "search_locations",
        {"body": {"q": "Chicago"}},
    ),
    "liteapi": lambda: asyncio.run(check_hotels()),
}


def main() -> int:
    failed = 0
    for name, check in CHECKS.items():
        try:
            print(f"PASS  {name}: {check()}")
        except Exception as e:
            failed += 1
            # Never print the exception's URL: LiteAPI's key is a query parameter.
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:300].split('apiKey=')[0]}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
