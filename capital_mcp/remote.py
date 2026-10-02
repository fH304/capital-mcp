"""Private, demo-only HTTP deployment with GitHub OAuth."""
import os

from fastmcp import FastMCP
from fastmcp.server.auth import AuthContext
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.middleware import AuthMiddleware
from starlette.responses import PlainTextResponse

# Override before importing the original server/configuration.
os.environ["CAP_ENV"] = "demo"
os.environ["CAP_ALLOW_TRADING"] = "false"

from . import server as original
from .config import get_config

SAFE_TOOLS = (
    "cap_session_status", "cap_session_login", "cap_session_ping",
    "cap_market_search", "cap_market_get", "cap_market_navigation_root",
    "cap_market_navigation_node", "cap_market_prices", "cap_market_sentiment",
    "cap_account_list", "cap_account_preferences_get",
    "cap_account_history_activity", "cap_account_history_transactions",
    "cap_trade_positions_list", "cap_trade_positions_get", "cap_trade_orders_list",
    "cap_watchlists_list", "cap_watchlists_get",
)


def owner_only(ctx: AuthContext) -> bool:
    return bool(ctx.token and str(ctx.token.claims.get("login", "")).casefold()
                == os.environ["GITHUB_OWNER"].casefold())


def build_server() -> FastMCP:
    get_config()  # Fail before listening if Capital credentials are missing.
    if not os.environ.get("GITHUB_OWNER", "").strip():
        raise ValueError("GITHUB_OWNER is required")
    base_url = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("PUBLIC_BASE_URL must use HTTPS")
    auth = GitHubProvider(
        client_id=os.environ["GITHUB_CLIENT_ID"],
        client_secret=os.environ["GITHUB_CLIENT_SECRET"],
        base_url=base_url,
        jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
    )
    app = FastMCP("Capital Demo Read Only", auth=auth,
                  middleware=[AuthMiddleware(auth=owner_only)])
    for name in SAFE_TOOLS:
        app.tool(getattr(original, name), auth=owner_only)

    @app.custom_route("/health", methods=["GET"])
    async def health(request):
        return PlainTextResponse("OK")

    return app


if __name__ == "__main__":
    build_server().run(transport="http", host="0.0.0.0",
                       port=int(os.environ.get("PORT", "10000")))
