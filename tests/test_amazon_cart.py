"""Amazon adapter regressions against local HTML, with no Amazon requests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from jarvis import amazon_tools
from jarvis.amazon_tools import (
    AMAZON_TOOLS,
    CartArgs,
    amazon_product_snapshot,
    amazon_search_snapshot,
    cart_snapshot,
    select_option,
    wait_product,
)
from jarvis.tools import ToolFailure

ASIN = "B012345678"
CHILD_ASIN = "B087654321"
ARGS = {
    "asin": ASIN,
    "title": "Cotton shirt",
    "size": "M",
    "color": "Black",
    "unit_price": 599,
    "max_price": 700,
}
PRODUCT = {
    "asin": ASIN,
    "title": "Cotton shirt",
    "size": "M",
    "color": "Black",
    "price": 599,
    "currency": "INR",
    "sizes": [{"label": "M", "asin": ASIN}],
    "colors": [{"label": "Black", "asin": ASIN}],
    "can_add": True,
}
PRODUCT_HTML = f"""
<input id="ASIN" value="{ASIN}">
<h1 id="productTitle">Cotton shirt</h1>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">₹599.00</span></span>
</div>
<div id="variation_size_name"><span class="selection">M</span></div>
<div id="variation_color_name"><span class="selection">Black</span></div>
<select id="native_dropdown_selected_size_name">
  <option value="-1">Select</option><option value="0,{ASIN}" selected>M</option>
  <option value="1,{CHILD_ASIN}">L</option>
</select>
<button id="add-to-cart-button">Add to cart</button>
"""


def test_cart_schema_rejects_price_above_approved_budget():
    with pytest.raises(ValueError, match="exceeds the approved maximum"):
        CartArgs(**{**ARGS, "max_price": 598})
    assert CartArgs(**{**ARGS, "max_price": 599}).unit_price == 599


@pytest.fixture
async def fixture_page():
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True)
        context = await browser.new_context()
        # Prevent every network request, even if a fixture accidentally adds one.
        await context.route("**/*", lambda route: route.abort())
        page = await context.new_page()
        yield page
        await browser.close()


async def test_search_deduplicates_asins_before_applying_limit(fixture_page):
    await fixture_page.set_content(
        "".join(
            f'<div data-component-type="s-search-result" data-asin="{asin}">'
            '<h2>Shirt</h2><span class="a-price"><span class="a-offscreen">₹599</span>'
            "</span></div>"
            for asin in [ASIN] * 13 + [CHILD_ASIN, "bad-id"]
        )
    )
    results = await amazon_search_snapshot(fixture_page)
    assert [item["asin"] for item in results] == [ASIN, CHILD_ASIN]


async def test_product_reads_modern_price_and_child_variant_options(fixture_page):
    await fixture_page.set_content(PRODUCT_HTML)
    product = await amazon_product_snapshot(fixture_page)
    assert product["price"] == 599
    assert product["sizes"] == [
        {"label": "M", "asin": ASIN},
        {"label": "L", "asin": CHILD_ASIN},
    ]


async def test_native_color_dropdown_is_reported_as_selected_variant(fixture_page):
    await fixture_page.set_content(
        PRODUCT_HTML.replace(
            '<div id="variation_color_name"><span class="selection">Black</span></div>',
            f'<select id="native_dropdown_selected_color_name">'
            f'<option value="0,{ASIN}" selected>Black</option>'
            f'<option value="1,{CHILD_ASIN}">White</option></select>',
        )
    )
    product = await amazon_product_snapshot(fixture_page)
    assert product["color"] == "Black"
    assert product["colors"] == [
        {"label": "Black", "asin": ASIN},
        {"label": "White", "asin": CHILD_ASIN},
    ]


async def test_selection_waits_for_child_asin_and_price_update(fixture_page):
    await fixture_page.set_content(PRODUCT_HTML)
    await fixture_page.evaluate(
        """child => {
          const select = document.querySelector('select');
          select.style.display = 'none';
          select.addEventListener('change', () => {
            document.querySelector('#variation_size_name .selection').textContent = 'L';
            setTimeout(() => {
              document.querySelector('#ASIN').value = child;
              document.querySelector('.a-offscreen').textContent = '₹649';
            }, 900);
          });
        }""",
        CHILD_ASIN,
    )
    await select_option(fixture_page, "size", "L")
    product = await amazon_product_snapshot(fixture_page)
    assert product["asin"] == CHILD_ASIN
    assert product["price"] == 649
    assert product["size"] == "L"


async def test_product_readiness_waits_for_rendered_title(fixture_page):
    await fixture_page.set_content("<body>Loading</body>")
    await fixture_page.evaluate(
        """html => setTimeout(() => { document.body.innerHTML = html; }, 100)""",
        PRODUCT_HTML,
    )
    await wait_product(fixture_page)
    assert (await amazon_product_snapshot(fixture_page))["asin"] == ASIN


@pytest.mark.parametrize(
    "quantity_html",
    [
        '<span data-a-selector="value">1</span>',
        '<select name="quantity"><option selected>1</option></select>',
        '<input name="quantity" value="1">',
    ],
)
async def test_cart_reads_current_and_legacy_quantity_controls(fixture_page, quantity_html):
    await fixture_page.set_content(
        f'<div id="sc-active-cart"><div class="sc-list-item" data-asin="{ASIN}">'
        f'{quantity_html}<span class="sc-product-price">₹599.00</span></div></div>'
    )
    result = await cart_snapshot(fixture_page, ASIN)
    assert result["quantity"] == 1
    assert result["unit_price"] == 599


async def test_saved_for_later_is_not_counted_as_active_cart(fixture_page):
    await fixture_page.set_content(
        f'<div id="sc-saved-cart"><div class="sc-list-item" data-asin="{ASIN}" '
        'data-quantity="1"><span class="sc-product-price">₹599</span></div></div>'
    )
    assert await cart_snapshot(fixture_page, ASIN) is None


async def setup_mock_cart(store, monkeypatch, *, existing=None, unreadable=False):
    click = AsyncMock()
    control = SimpleNamespace(first=SimpleNamespace(click=click))
    product_page = SimpleNamespace(goto=AsyncMock(), locator=lambda _: control)
    cart_page = SimpleNamespace(
        locator=lambda _: SimpleNamespace(count=AsyncMock(return_value=int(unreadable)))
    )

    async def page(name):
        return cart_page if name.startswith("cart-") else product_page

    store.browser_session = SimpleNamespace(lock=asyncio.Lock(), page=page)
    monkeypatch.setattr(amazon_tools, "wait_product", AsyncMock())
    monkeypatch.setattr(amazon_tools, "select_option", AsyncMock())
    monkeypatch.setattr(amazon_tools, "open_cart", AsyncMock())
    monkeypatch.setattr(
        amazon_tools, "amazon_product_snapshot", AsyncMock(return_value=dict(PRODUCT))
    )
    monkeypatch.setattr(amazon_tools, "cart_snapshot", AsyncMock(return_value=existing))
    await store.db.browser_observations.insert_one({"_id": f"task:{ASIN}", **PRODUCT})
    return click


@pytest.mark.parametrize("unreadable", [False, True])
async def test_existing_mismatched_or_unreadable_line_is_never_added_again(
    store, monkeypatch, unreadable
):
    existing = None if unreadable else {"quantity": 1, "unit_price": 650}
    click = await setup_mock_cart(store, monkeypatch, existing=existing, unreadable=unreadable)
    with pytest.raises(ToolFailure, match="already in the cart"):
        await AMAZON_TOOLS["amazon.add_to_cart"].run(ARGS, "task:s1", store)
    click.assert_not_awaited()
    assert await store.db.browser_dispatches.count_documents({}) == 0


async def test_exact_existing_variant_is_verified_without_increasing_quantity(store, monkeypatch):
    click = await setup_mock_cart(
        store, monkeypatch, existing={"asin": ASIN, "quantity": 2, "unit_price": 599}
    )
    result = await AMAZON_TOOLS["amazon.add_to_cart"].run(ARGS, "task:s1", store)
    assert result["already_present"]
    click.assert_not_awaited()
    assert await AMAZON_TOOLS["amazon.add_to_cart"].verify(ARGS, result, store)


async def test_submission_fence_prevents_duplicate_even_before_readback(store, monkeypatch):
    click = await setup_mock_cart(store, monkeypatch)
    tool = AMAZON_TOOLS["amazon.add_to_cart"]
    await tool.run(ARGS, "task:s1", store)
    with pytest.raises(ToolFailure, match="earlier cart addition") as error:
        await tool.run(ARGS, "task:s1", store)
    assert not error.value.retry_allowed
    click.assert_awaited_once()
    assert (await store.db.browser_dispatches.find_one({"_id": "task:s1"}))["args"] == ARGS


async def test_delayed_cart_visibility_reloads_without_repeating_add(store, monkeypatch):
    click = await setup_mock_cart(store, monkeypatch)
    snapshots = AsyncMock(
        side_effect=[None, None, {"asin": ASIN, "quantity": 1, "unit_price": 599}]
    )
    monkeypatch.setattr(amazon_tools, "cart_snapshot", snapshots)
    monkeypatch.setattr(amazon_tools, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    assert await AMAZON_TOOLS["amazon.add_to_cart"].verify(ARGS, {"id": "task:s1"}, store)
    assert amazon_tools.open_cart.await_count == 3
    click.assert_not_awaited()


async def test_price_change_while_cart_loads_requires_new_inspection(store, monkeypatch):
    click = await setup_mock_cart(store, monkeypatch)
    monkeypatch.setattr(
        amazon_tools,
        "amazon_product_snapshot",
        AsyncMock(side_effect=[dict(PRODUCT), {**PRODUCT, "price": 699}]),
    )
    with pytest.raises(ToolFailure, match="changed the offer"):
        await AMAZON_TOOLS["amazon.add_to_cart"].run(ARGS, "task:s1", store)
    click.assert_not_awaited()
    assert await store.db.browser_dispatches.count_documents({}) == 0


async def test_full_inspect_add_and_readback_against_local_storefront(store, fixture_page):
    """Exercise actual selectors, events and navigation with HTTP fully intercepted."""
    context = fixture_page.context
    cart_page = await context.new_page()
    state = {"quantity": 0, "add_requests": 0}

    async def storefront(route):
        url = route.request.url
        if "/fixture-add" in url:
            state["quantity"] += 1
            state["add_requests"] += 1
            return await route.fulfill(status=200, body="ok")
        if "/gp/cart/" in url:
            lines = (
                f'<div class="sc-list-item" data-asin="{ASIN}">'
                f'<span data-a-selector="value">{state["quantity"]}</span>'
                '<span class="sc-product-price">₹599.00</span></div>'
                if state["quantity"]
                else ""
            )
            html = f'<div id="sc-active-cart">{lines}</div>'
        elif "/dp/" in url:
            html = PRODUCT_HTML.replace(
                '<button id="add-to-cart-button">',
                '<button id="add-to-cart-button" onclick="fetch(\'/fixture-add\')">',
            )
        else:
            return await route.abort()
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    await context.route("https://www.amazon.in/**", storefront)

    async def page(name):
        return cart_page if name.startswith("cart-") else fixture_page

    store.browser_session = SimpleNamespace(
        lock=asyncio.Lock(), page=page, pages={"amazon-task": fixture_page}
    )
    inspect = AMAZON_TOOLS["amazon.inspect"]
    result = await inspect.run({"asin": ASIN, "size": "M", "color": "Black"}, "task:s0", store)
    assert await inspect.verify({}, result, store)
    add = AMAZON_TOOLS["amazon.add_to_cart"]
    result = await add.run(ARGS, "task:s1", store)
    assert await add.verify(ARGS, result, store)
    assert state == {"quantity": 1, "add_requests": 1}
    # A later request for the same already-present item must not increment it.
    repeat = await add.run(ARGS, "task:s2", store)
    assert repeat["already_present"]
    assert state == {"quantity": 1, "add_requests": 1}
