"""
mcp_server.py — Bella Pet MCP Server
يشتغل كـ HTTP client على REST API المحلي — لا imports نسبية، لا مشاكل مسار.

إعداد Claude Desktop / Claude Code (.claude/settings.json):
{
  "mcpServers": {
    "bella-pet": {
      "command": "python",
      "args": ["C:/Users/Admin/Downloads/petstore/petstore/mcp_server.py"],
      "env": {
        "BELLA_API_KEY": "your-key-here",
        "BELLA_API_URL": "http://127.0.0.1:5000"
      }
    }
  }
}
"""

import os
import json
import urllib.request
import urllib.error
import urllib.parse
import asyncio

import mcp.server.stdio
from mcp.server import Server
from mcp.types import Tool, TextContent

# ── Config من البيئة — لا hardcoded keys ─────────────────────────
API_KEY = os.environ.get("BELLA_API_KEY", "")
API_URL = os.environ.get("BELLA_API_URL", "http://127.0.0.1:5000").rstrip("/")

if not API_KEY:
    import sys
    print("❌ BELLA_API_KEY environment variable is not set.", file=sys.stderr)
    sys.exit(1)

server = Server("bella-pet")


# ── HTTP helper ───────────────────────────────────────────────────

def _call(method: str, path: str, body: dict | None = None) -> dict | list:
    """استدعاء REST API المحلي."""
    url     = f"{API_URL}{path}"
    payload = json.dumps(body).encode() if body else None
    req     = urllib.request.Request(
        url,
        data    = payload,
        method  = method,
        headers = {
            "X-API-Key":    API_KEY,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')}"}
    except Exception as e:
        return {"error": str(e)}


def _text(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


# ── Tool definitions ──────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_products",
            description=(
                "ابحث عن منتجات في Bella Pet بالعربية أو الإنجليزية. "
                "يمكن الفلترة حسب نوع الحيوان (pet)، الفئة، أو الحد الأقصى للسعر."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query":     {"type": "string",  "description": "كلمة بحث (اسم منتج، ماركة...)"},
                    "pet":       {"type": "string",  "description": "dog | cat | bird | fish | small"},
                    "category":  {"type": "string",  "description": "food | treats | accessories | litter | ..."},
                    "max_price": {"type": "number",  "description": "الحد الأقصى للسعر $"},
                    "limit":     {"type": "integer", "description": "عدد النتائج (افتراضي 10، أقصى 50)"},
                },
            },
        ),
        Tool(
            name="get_product",
            description="احصل على تفاصيل كاملة لمنتج معين بالـ ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                },
                "required": ["product_id"],
            },
        ),
        Tool(
            name="ask_advisor",
            description=(
                "اسأل بيتي (مستشار AI) عن توصية منتج مناسب. "
                "يرجع رد بالعامية اللبنانية + منتجات مقترحة مع أسعار."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "سؤال أو طلب الزبون"},
                    "history": {
                        "type": "array",
                        "description": "تاريخ المحادثة السابق (اختياري)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role":    {"type": "string", "enum": ["user", "assistant"]},
                                "content": {"type": "string"},
                            },
                        },
                    },
                    "lang": {"type": "string", "description": "ar (افتراضي) أو en"},
                },
                "required": ["message"],
            },
        ),
        Tool(
            name="place_order",
            description=(
                "ضع طلب شراء مباشرة. "
                "مطلوب: اسم الزبون، رقم الهاتف، المنطقة، وقائمة المنتجات. "
                "يرجع رقم الطلب والمجموع."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "phone":         {"type": "string", "description": "مثال: 03123456"},
                    "area":          {"type": "string", "description": "بيروت، جبل لبنان، الشمال..."},
                    "address_note":  {"type": "string", "description": "تفاصيل العنوان (اختياري)"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "integer"},
                                "qty":        {"type": "integer", "minimum": 1},
                            },
                            "required": ["product_id", "qty"],
                        },
                    },
                },
                "required": ["customer_name", "phone", "area", "items"],
            },
        ),
        Tool(
            name="get_promotions",
            description="احصل على العروض النشطة وطبقات الأسعار الحالية في المتجر.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_order_status",
            description="تتبع طلب أو طلبات الزبون برقم هاتفه.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "رقم هاتف الزبون"},
                },
                "required": ["phone"],
            },
        ),
    ]


# ── Tool execution ────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:

    if name == "search_products":
        params = []
        if arguments.get("query"):     params.append(f"q={urllib.parse.quote(arguments['query'])}")
        if arguments.get("pet"):       params.append(f"pet={arguments['pet']}")
        if arguments.get("category"):  params.append(f"category={arguments['category']}")
        if arguments.get("max_price"): params.append(f"max_price={arguments['max_price']}")
        params.append(f"limit={min(int(arguments.get('limit', 10)), 50)}")
        qs = "&".join(params)
        return _text(_call("GET", f"/api/v1/products?{qs}"))

    elif name == "get_product":
        return _text(_call("GET", f"/api/v1/products/{int(arguments['product_id'])}"))

    elif name == "ask_advisor":
        body = {
            "message": arguments["message"],
            "history": arguments.get("history", []),
            "lang":    arguments.get("lang", "ar"),
        }
        return _text(_call("POST", "/api/v1/advisor", body))

    elif name == "place_order":
        return _text(_call("POST", "/api/v1/orders", arguments))

    elif name == "get_promotions":
        return _text(_call("GET", "/api/v1/promotions"))

    elif name == "get_order_status":
        phone = urllib.parse.quote(arguments["phone"])
        return _text(_call("GET", f"/api/v1/orders?phone={phone}"))

    return _text({"error": f"unknown tool: {name}"})


# ── Entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(mcp.server.stdio.run(server))
