"""Plain REST demonstration service: intentionally no MCP dependency or route."""
import hmac
import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=200)
    stock: int = Field(ge=0)
    price: float = Field(ge=0)


def inventory_app(folder: Path):
    token = os.environ.get("INVENTORY_API_TOKEN")
    if not token:
        raise ValueError("Set INVENTORY_API_TOKEN before starting the REST demo")
    folder.mkdir(parents=True, exist_ok=True)
    database = folder / "inventory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, name TEXT, stock INTEGER, price REAL)")
        connection.executemany("INSERT OR IGNORE INTO items VALUES (?, ?, ?, ?)", [
            ("widget", "Widget", 12, 3.5), ("gadget", "Gadget", 4, 8.0), ("bolt", "Bolt", 0, 0.5)])

    def rows(sql, values):
        with sqlite3.connect(database) as connection:
            return [dict(zip(("id", "name", "stock", "price"), row)) for row in connection.execute(sql, values)]

    async def health(request):
        return JSONResponse({"status": "ready", "transport": "rest"})

    async def list_items(request):
        try:
            minimum = int(request.query_params.get("min_stock", 0))
            limit = int(request.query_params.get("limit", 10))
            query = request.query_params.get("q", "")
            if minimum < 0 or not 1 <= limit <= 50 or len(query) > 200:
                raise ValueError()
        except ValueError:
            return JSONResponse({"error": "Invalid query"}, status_code=400)
        items = rows("SELECT id, name, stock, price FROM items WHERE stock >= ? AND instr(lower(name), lower(?)) > 0 ORDER BY id LIMIT ?", (minimum, query, limit))
        return JSONResponse({"items": items, "count": len(items)})

    async def get_item(request):
        items = rows("SELECT id, name, stock, price FROM items WHERE id = ?", (request.path_params["item_id"],))
        return JSONResponse(items[0] if items else {"error": "Not found"}, status_code=200 if items else 404)

    async def create_item(request):
        try:
            item = Item.model_validate(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse({"error": "Invalid item"}, status_code=400)
        try:
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO items VALUES (?, ?, ?, ?)", (item.id, item.name, item.stock, item.price))
        except sqlite3.IntegrityError:
            return JSONResponse({"error": "Item already exists"}, status_code=409)
        return JSONResponse(item.model_dump(), status_code=201)

    app = Starlette(routes=[Route("/healthz", health), Route("/v1/items", list_items),
        Route("/v1/items", create_item, methods=["POST"]), Route("/v1/items/{item_id}", get_item)])

    async def secured(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            actual = headers.get(b"authorization", b"")
            if not hmac.compare_digest(actual, ("Bearer " + token).encode()):
                return await JSONResponse({"error": "Service bearer token required"}, status_code=401)(scope, receive, send)
        return await app(scope, receive, send)
    return secured
