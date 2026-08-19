"""Sunset mode: the retired gateway must stop signing users in and answer every
MCP call with the moved-to-missingmcp.com notice — without spawning workers."""
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from garmin_gateway import store, oauth, proxy, security, workers
from garmin_gateway.config import load_config

CONFIG = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com"})


def test_sunset_defaults_on():
    assert CONFIG.sunset is True
    off = load_config({"GATEWAY_SECRET": "s" * 40, "GATEWAY_SUNSET": "0"})
    assert off.sunset is False


# --- /oauth/authorize ---------------------------------------------------------

def _authorize_client(conn):
    state = oauth.AuthState(security.CsrfStore())

    async def aget(request):
        return await oauth.authorize_get(request, None, state, conn, CONFIG)

    async def apost(request):
        return await oauth.authorize_post(request, None, state, conn, CONFIG)

    return TestClient(Starlette(routes=[
        Route("/oauth/authorize", aget, methods=["GET"]),
        Route("/oauth/authorize", apost, methods=["POST"]),
    ]))


def test_authorize_get_shows_moved_page_instead_of_form(tmp_path):
    conn = store.init_db(":memory:")
    store.create_client(conn, "c1", store.hash_token("sec"), ["https://claude.ai/cb"], "n")
    c = _authorize_client(conn)
    r = c.get("/oauth/authorize", params={
        "client_id": "c1", "redirect_uri": "https://claude.ai/cb",
        "state": "st", "code_challenge": "x" * 43, "code_challenge_method": "S256",
    })
    assert r.status_code == 200
    assert "missingmcp.com" in r.text
    assert "garmin_password" not in r.text        # no login form to fill


def test_authorize_post_never_signs_in(tmp_path):
    conn = store.init_db(":memory:")
    c = _authorize_client(conn)
    r = c.post("/oauth/authorize", data={"csrf": "whatever",
                                         "garmin_email": "a@b.c", "garmin_password": "p"})
    assert r.status_code == 200
    assert "missingmcp.com" in r.text


# --- /mcp ---------------------------------------------------------------------

class FakeProc:
    def poll(self): return None
    def terminate(self): pass


def _mcp_client(tmp_path):
    conn = store.init_db(":memory:")
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://x",
                       "DATA_DIR": str(tmp_path)})
    token = "tok-sunset"
    store.upsert_account(conn, "me@x.cz", '{"t":1}', cfg.gateway_secret)
    store.create_access_token(conn, store.hash_token(token), "me@x.cz", "c1")
    spawned = []
    mgr = workers.WorkerManager(cfg, spawn=lambda *a: spawned.append(a) or FakeProc())
    rate = security.RateLimiter()

    async def mcp_post(request):
        return await proxy.handle_mcp(request, "POST", conn, mgr, cfg, cfg.gateway_secret, rate)

    client = TestClient(Starlette(routes=[Route("/mcp", mcp_post, methods=["POST"])]))
    auth = {"Authorization": f"Bearer {token}"}
    return client, auth, conn, spawned


def test_sunset_still_requires_auth(tmp_path):
    client, _auth, _conn, _spawned = _mcp_client(tmp_path)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_initialize_answers_with_instructions_and_no_worker(tmp_path):
    client, auth, _conn, spawned = _mcp_client(tmp_path)
    r = client.post("/mcp", headers=auth, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "missingmcp.com" in result["instructions"]
    assert spawned == []                          # no garmin-mcp subprocess


def test_tools_list_offers_only_the_moved_notice(tmp_path):
    client, auth, _conn, _spawned = _mcp_client(tmp_path)
    r = client.post("/mcp", headers=auth,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r.json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["connector_moved"]
    assert "missingmcp.com" in tools[0]["description"]


def test_tools_call_returns_moved_error_result(tmp_path):
    client, auth, conn, spawned = _mcp_client(tmp_path)
    r = client.post("/mcp", headers=auth, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "get_activities", "arguments": {}}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert "missingmcp.com" in result["content"][0]["text"]
    assert spawned == []
    # last-used tracking keeps working so scripts/status shows the stragglers
    row = conn.execute("SELECT tool, calls FROM tool_usage WHERE garmin_user_key='me@x.cz'").fetchone()
    assert row is not None and row[0] == "get_activities"


def test_notification_gets_202(tmp_path):
    client, auth, _conn, _spawned = _mcp_client(tmp_path)
    r = client.post("/mcp", headers=auth,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
