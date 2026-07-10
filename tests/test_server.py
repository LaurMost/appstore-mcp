from pathlib import Path

import httpx
import pytest
from dirty_equals import IsDatetime
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from inline_snapshot import snapshot

from appstore_mcp.server import _auth_provider, create_server

FIXTURES = Path(__file__).parent / "fixtures"


def apple_transport(page_status: int = 200) -> httpx.MockTransport:
    """Route requests to the recorded fixture matching the real Apple endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if request.url.host == "apps.apple.com":
            if page_status != 200:
                return httpx.Response(page_status)
            return httpx.Response(
                200, content=(FIXTURES / "page_duolingo_us.html").read_bytes()
            )
        if path == "/search":
            return httpx.Response(
                200,
                content=(FIXTURES / "search_language_learning_us.json").read_bytes(),
            )
        if path == "/lookup":
            ids = params.get("id", "")
            if ids == "570060128":
                name = "lookup_duolingo_us.json"
            elif "," in ids:
                name = "lookup_multi_us.json"
            else:
                name = "lookup_notfound.json"
            return httpx.Response(200, content=(FIXTURES / name).read_bytes())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def server_client() -> Client:
    http = httpx.AsyncClient(transport=apple_transport())
    return Client(create_server(http=http))


async def test_search_tool_returns_slim_results(server_client: Client) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "search_app_store", {"query": "language learning", "limit": 10}
        )
    data = result.structured_content
    meta = data["meta"]
    # retrieved_at is the one runtime-dynamic field; assert it flexibly, then
    # snapshot the rest of meta (fixed by the fixture) verbatim.
    assert meta["retrieved_at"] == IsDatetime(iso_string=True)
    assert {k: v for k, v in meta.items() if k != "retrieved_at"} == snapshot(
        {
            "store": "apple_app_store",
            "country": "us",
            "language": None,
            "fresh": True,
            "warnings": [],
        }
    )
    # The full snapshot documents the slim shape (no description/screenshots).
    assert data["results"][0] == snapshot(
        {
            "app_id": "570060128",
            "name": "Duolingo: Language Lessons",
            "developer": "Duolingo",
            "bundle_id": "com.duolingo.DuolingoMobile",
            "url": "https://apps.apple.com/us/app/duolingo-language-lessons/id570060128?uo=4",
            "price": 0.0,
            "currency": "USD",
            "rating": 4.72588,
            "rating_count": 5309734,
            "primary_genre": "Education",
        }
    )
    source = data["sources"][0]
    assert source["name"] == "apple_itunes_api"
    # The reported URL must reproduce the actual request, entity param included.
    assert "entity=software" in source["url"]


async def test_get_app_returns_full_profile_without_raw(server_client: Client) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "get_app_store_app", {"app_id_or_url": "570060128"}
        )
    data = result.structured_content
    # The full profile is entirely fixture-derived (no runtime-dynamic fields),
    # so snapshot the whole app shape rather than spot-checking a few fields.
    assert data["app"] == snapshot(
        {
            "app_id": "570060128",
            "name": "Duolingo: Language Lessons",
            "developer": "Duolingo",
            "bundle_id": "com.duolingo.DuolingoMobile",
            "url": "https://apps.apple.com/us/app/duolingo-language-lessons/id570060128?uo=4",
            "price": 0.0,
            "currency": "USD",
            "rating": 4.72588,
            "rating_count": 5309444,
            "primary_genre": "Education",
            "description": """\
Learn a new language, chess & more with the world's most downloaded education app! Duolingo is the fun, free app for learning 40+ languages through quick, bite-sized lessons. Practice speaking, reading, listening & writing to build your vocabulary & grammar skills.

Designed by learning experts & loved by hundreds of millions worldwide, our lessons make learning effective & fun and help you prepare for real conversations in Spanish, French, Japanese, Korean, Chinese, Italian, German, English and more.


And now, you can learn CHESS on Duolingo! Whether you're a total beginner or looking to level up your game, you'll love learning chess the Duolingo way. Play matches and fun chess lessons for all levels, no matter the language - chess, ajedrez, xadrez, schach, Шахматы, الشطرنج.

• Chess: Learn the moves, level up your game & play matches in our Chess course. Learn the basics, solve fun chess puzzles & improve your strategy with guided lessons. Whether you're new to chess or already play, our course is designed to help you have fun and build real skills. Checkmate!

• Math: Forget lectures and math worksheets! Built by learning experts, our Math course makes math feel like a game. Boost learning and fight the summer slide with standards-aligned math lessons for elementary school, middle school, and high school students.

• Music: Learn how to read music & play songs in our Music course, no instrument needed! Using an on-screen keyboard, you'll learn bit-by-bit.

Whether you're learning a language for travel, school, career or your brain health, you'll love learning with Duolingo.


Why Duolingo?

• Duolingo is fun & effective. Game-like language lessons & fun characters help you build speaking, reading, listening, & writing skills, plus enjoy fun chess & competitive online chess in one app.

• Duolingo works. Designed by learning experts, Duolingo has a science-based teaching methodology proven to foster long-term knowledge retention across language lessons & chess lessons alike.

• Track your progress. Work toward your learning goals with playful rewards & achievements when you make practicing language lessons or chess online part of your daily habit.

• Join millions of learners. Stay motivated with competitive Leaderboards as you learn languages & play chess online alongside our global community.

• Every course is free. Learn Spanish, French, German, Italian, Russian, Portuguese, Turkish, Dutch, Irish, Danish, Swedish, Ukrainian, Esperanto, Polish, Greek, Hungarian, Norwegian, Hebrew, Welsh, Arabic, Latin, Hawaiian, Scottish Gaelic, Vietnamese, Korean, Japanese, English, & even High Valyrian! And now, learn Math, Music & improve your Chess skills with fun, bite-sized lessons.


What the world is saying about Duolingo:

"Far & away the best language-learning app." - The Wall Street Journal

"This free app & website is among the most effective language-learning methods I've tried… lessons come in the form of brief challenges, speaking, translating, answering multiple-choice questions, that keep me coming back for more." - The New York Times

"Duolingo may hold the secret to the future of education." - TIME Magazine

"Duolingo is cheerful, lighthearted & fun." - Forbes

"I Can't Stop Playing Duolingo Chess." - Wired

If you like Duolingo, try Super Duolingo for 14 days free! Learn a language fast with no ads & get fun perks like Unlimited Energy & Monthly Streak Repair.

If you choose to purchase Super Duolingo, payment will be charged to your Apple account, and your account will be charged for renewal within 24-hours prior to the end of the current period. Auto-renewal may be turned off at any time by going to your settings in the App Store after purchase. Any unused portion of a free trial period, if offered, will be forfeited when the user purchases a subscription to that publication, where applicable.

Privacy Policy: https://www.duolingo.com/privacy
Terms of Service: https://www.duolingo.com/terms\
""",
            "release_notes": """\
Our mascot owl, Duo, is trading in his usual diet of mice for bugs this week. Yup, we’re squashing those pesky bugs to make the experience better for all of you.

For more Duolingo news, contests and product releases, follow us on Facebook, Twitter, and Instagram @duolingo.\
""",
            "version": "7.130.0",
            "release_date": "2012-11-13T08:00:00Z",
            "last_updated": "2026-07-07T14:30:14Z",
            "min_os_version": "17.0",
            "content_rating": "4+",
            "genres": ["Education", "Social Networking"],
            "languages": [
                "AR",
                "BN",
                "CS",
                "NL",
                "EN",
                "FR",
                "DE",
                "EL",
                "HI",
                "HU",
                "ID",
                "IT",
                "JA",
                "KO",
                "PL",
                "PT",
                "PA",
                "RO",
                "RU",
                "ZH",
                "ES",
                "SV",
                "TA",
                "TE",
                "TH",
                "ZH",
                "TR",
                "UK",
                "UR",
                "VI",
            ],
            "device_families": ["iphone", "ipad", "ipod"],
            "icon_url": "https://is1-ssl.mzstatic.com/image/thumb/Purple221/v4/16/04/41/1604410f-28d5-9116-cbca-623f1cb6a32b/AppIcon-0-0-1x_U007epad-0-1-85-220.png/512x512bb.jpg",
            "screenshot_urls": [],
            "ipad_screenshot_count": 0,
            "subtitle": "Languages, Math, Music & Chess",
            "has_iap": True,
            "privacy": [
                {
                    "identifier": "DATA_USED_TO_TRACK_YOU",
                    "title": "Data Used to Track You",
                    "detail": "The following data may be used to track you across apps and websites owned by other companies:",
                    "categories": [
                        "Purchases",
                        "Location",
                        "Contact Info",
                        "User Content",
                        "Identifiers",
                        "Usage Data",
                        "Diagnostics",
                        "Other Data",
                    ],
                    "purposes": [],
                },
                {
                    "identifier": "DATA_LINKED_TO_YOU",
                    "title": "Data Linked to You",
                    "detail": "The following data may be collected and linked to your identity:",
                    "categories": [
                        "Purchases",
                        "Financial Info",
                        "Location",
                        "Contact Info",
                        "Contacts",
                        "User Content",
                        "Search History",
                        "Identifiers",
                        "Usage Data",
                        "Diagnostics",
                        "Other Data",
                    ],
                    "purposes": [],
                },
            ],
            "description_length": 3974,
            "screenshot_count": 0,
        }
    )
    assert data["raw"] is None


async def test_get_app_include_raw_returns_lookup_payload(
    server_client: Client,
) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "get_app_store_app", {"app_id_or_url": "570060128", "include_raw": True}
        )
    raw = result.structured_content["raw"]
    assert raw["itunes_lookup"]["trackId"] == 570060128


@pytest.mark.parametrize(
    ("app_id_or_url", "expected_country"),
    [
        ("570060128", "us"),
        ("https://apps.apple.com/de/app/x/id570060128", "de"),
        ("https://apps.apple.com/app/id570060128", "us"),
    ],
)
async def test_get_app_resolves_id_and_country_across_ref_formats(
    server_client: Client, app_id_or_url: str, expected_country: str
) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "get_app_store_app", {"app_id_or_url": app_id_or_url}
        )
    data = result.structured_content
    assert data["app"]["app_id"] == "570060128"
    assert data["meta"]["country"] == expected_country


async def test_get_app_merges_page_enrichment_by_default(server_client: Client) -> None:
    async with server_client as client:
        result = await client.call_tool(
            "get_app_store_app", {"app_id_or_url": "570060128"}
        )
    data = result.structured_content
    app = data["app"]
    assert app["subtitle"] == "Languages, Math, Music & Chess"
    assert app["has_iap"] is True
    assert app["privacy"] is not None
    assert data["meta"]["warnings"] == []
    assert {s["name"] for s in data["sources"]} == {
        "apple_itunes_api",
        "apple_app_store_page",
    }


async def test_get_app_page_failure_degrades_with_warning() -> None:
    http = httpx.AsyncClient(transport=apple_transport(page_status=500))
    async with Client(create_server(http=http)) as client:
        result = await client.call_tool(
            "get_app_store_app", {"app_id_or_url": "570060128"}
        )
    data = result.structured_content
    assert data["app"]["app_id"] == "570060128"
    assert data["app"]["subtitle"] is None
    assert data["app"]["has_iap"] is None
    assert any("enrichment" in w for w in data["meta"]["warnings"])


async def test_get_app_can_skip_page_enrichment() -> None:
    hits: list[str] = []
    base = apple_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return base.handler(request)  # type: ignore[attr-defined]

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with Client(create_server(http=http)) as client:
        await client.call_tool(
            "get_app_store_app",
            {"app_id_or_url": "570060128", "include_page_data": False},
        )
    assert "apps.apple.com" not in hits


async def test_get_app_not_found_is_agent_recoverable_error(
    server_client: Client,
) -> None:
    async with server_client as client:
        with pytest.raises(Exception, match="per-country"):
            await client.call_tool(
                "get_app_store_app", {"app_id_or_url": "999999999999"}
            )


def test_auth_provider_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPSTORE_MCP_API_KEY", raising=False)
    assert _auth_provider() is None


def test_auth_provider_configures_static_token_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPSTORE_MCP_API_KEY", "secret-key")
    verifier = _auth_provider()
    assert isinstance(verifier, StaticTokenVerifier)
    assert verifier.tokens == {"secret-key": {"client_id": "hosted"}}


async def test_tools_carry_required_annotations(server_client: Client) -> None:
    async with server_client as client:
        tools = await client.list_tools()
    assert tools, "server exposes tools"
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.readOnlyHint is True, tool.name
        assert tool.annotations.openWorldHint is True, tool.name
        assert tool.annotations.title, tool.name
