"""Hotel workflow built with LangChain + LangGraph, tools loaded from LiteAPI's hosted MCP server.

Defaults to LiteAPI (free sandbox key). Set HOTEL_MCP_URL / HOTEL_MCP_HEADERS to swap providers.
LiteAPI takes the key as a URL parameter, so never log or print the URL.
"""
import asyncio
import json
import os
from datetime import date

import httpx
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

from agents.dates import with_today

HOTEL_PROMPT = """You are a hotel specialist. Search accommodations for the destination,
dates and guest count. Work in this order:
1. Find candidate hotels with get_data_hotels (limit 10-15) or get_data_hotels_semantic_search.
2. Get live prices for those hotel IDs with get_hotel_rates. Only hotels it returns are bookable.
3. Optionally check get_data_reviews for the strongest options. Names, addresses and
   ratings are already in the search results.
Return the 3 best matches with name, nightly and total price, rating and why it fits the
traveler's request. Only report what the tools return: if no rates come back, say so plainly
and don't present unpriced hotels as fitting the budget."""

# LiteAPI exposes ~90 tools, including booking, payment and voucher writes. Only hand the
# agent read-only search tools: fewer tokens per call, and it can't book or cancel anything.
# Its MCP rate tools (post_hotels_rates, post_hotels_min_rates) hang until timeout even for a
# few hotel IDs, while the same REST call answers in ~3s, so rates come from get_hotel_rates.
# get_data_hotel is left out too: it returns 60-250K characters per hotel (photos, every room).
LITEAPI_ALLOWED_TOOLS = {
    "get_data_places",                  # city/area name -> placeId
    "get_data_hotels",                  # hotels by city, place or coordinates
    "get_data_hotels_semantic_search",  # natural-language hotel search
    "get_data_reviews",                 # reviews for one hotel
}
LITEAPI_RATES_URL = "https://api.liteapi.travel/v3.0/hotels/rates"


def hotel_mcp_config() -> dict:
    return {
        "url": os.environ.get("HOTEL_MCP_URL")
        or f"https://mcp.liteapi.travel/api/mcp?apiKey={os.environ['LITEAPI_API_KEY']}",
        "transport": "streamable_http",
        "headers": json.loads(os.environ.get("HOTEL_MCP_HEADERS") or "{}"),
        "timeout": 30,
        "sse_read_timeout": 60,  # fail a hung tool call instead of waiting ~5 minutes
    }


def filter_hotel_tools(tools: list) -> list:
    """Apply the LiteAPI allowlist. A custom HOTEL_MCP_URL provider gets all of its tools."""
    if os.environ.get("HOTEL_MCP_URL"):
        return tools
    return [t for t in tools if t.name in LITEAPI_ALLOWED_TOOLS]


@tool
async def get_hotel_rates(
    hotel_ids: list[str], checkin: str, checkout: str, adults: int = 1, currency: str = "USD"
) -> list[dict] | str:
    """Get live, bookable prices for specific hotels, cheapest offer per hotel.

    Args:
        hotel_ids: Up to 20 LiteAPI hotel IDs, from get_data_hotels or semantic search.
        checkin: Check-in date, YYYY-MM-DD.
        checkout: Check-out date, YYYY-MM-DD.
        adults: Adults in one room.
        currency: ISO currency code for prices.
    """
    body = {
        "hotelIds": hotel_ids[:20],
        "checkin": checkin,
        "checkout": checkout,
        "occupancies": [{"adults": adults}],
        "currency": currency,
        "guestNationality": "US",
        "maxRatesPerHotel": 1,
        "timeout": 10,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            LITEAPI_RATES_URL, headers={"X-API-Key": os.environ["LITEAPI_API_KEY"]}, json=body
        )
    if resp.status_code >= 400:
        return f"Rate search failed ({resp.status_code}): {resp.text[:300]}"

    nights = max((date.fromisoformat(checkout) - date.fromisoformat(checkin)).days, 1)
    offers = []
    for hotel in resp.json().get("data", []):
        room = min(hotel["roomTypes"], key=lambda r: r["offerRetailRate"]["amount"])
        rate = room["rates"][0]
        total = room["offerRetailRate"]["amount"]
        offers.append({
            "hotel_id": hotel["hotelId"],
            "room": rate.get("name"),
            "board": rate.get("boardName"),
            "total_price": round(total, 2),
            "per_night": round(total / nights, 2),
            "currency": room["offerRetailRate"]["currency"],
            "refundable": rate.get("cancellationPolicies", {}).get("refundableTag") == "RFN",
        })
    return sorted(offers, key=lambda o: o["total_price"]) or "No availability for these hotels and dates."


async def _run_hotel_graph(request: str) -> str:
    client = MultiServerMCPClient({"hotels": hotel_mcp_config()})
    tools = filter_hotel_tools(await client.get_tools())
    if not os.environ.get("HOTEL_MCP_URL"):
        tools.append(get_hotel_rates)
    for t in tools:
        t.handle_tool_error = True  # give the model the error so it can retry or explain
    llm = ChatBedrockConverse(
        model=os.environ["BEDROCK_MODEL_ID"], region_name=os.environ.get("AWS_REGION", "us-east-1")
    )
    graph = create_react_agent(llm, tools, prompt=with_today(HOTEL_PROMPT))
    result = await graph.ainvoke({"messages": [("user", request)]})
    return result["messages"][-1].content


def search_hotels(request: str) -> str:
    """Sync wrapper so the Strands supervisor can call the LangGraph workflow as a tool."""
    return asyncio.run(_run_hotel_graph(request))
