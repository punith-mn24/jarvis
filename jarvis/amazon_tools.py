"""Amazon India clothing search, variant selection and verified cart actions."""

import asyncio
import re
from time import monotonic
from urllib.parse import urlencode

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from pydantic import Field, model_validator
from pymongo.errors import DuplicateKeyError

from jarvis.browser_runtime import BrowserTool, first_text, session
from jarvis.models import FailureClass, now
from jarvis.tools import Args, ToolFailure

PRODUCT_READY = '#productTitle, form[action*="validateCaptcha"], #captchacharacters, #ap_email'
CART_READY = "#sc-active-cart, #sc-empty-cart, #sc-empty-cart-section, #activeCartViewForm"
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")


class SearchArgs(Args):
    query: str = Field(min_length=3, max_length=300)


class InspectArgs(Args):
    asin: str = Field(pattern=r"^[A-Z0-9]{10}$")
    size: str = Field(default="", max_length=100)
    color: str = Field(default="", max_length=100)


class CartArgs(InspectArgs):
    title: str = Field(min_length=1, max_length=500)
    unit_price: float = Field(gt=0, le=1000000)
    max_price: float = Field(gt=0, le=1000000)

    @model_validator(mode="after")
    def price_within_budget(self):
        if self.unit_price > self.max_price:
            raise ValueError("The unit price exceeds the approved maximum price.")
        return self


def price_number(value: str) -> float | None:
    match = re.search(r"(?:₹|INR|Rs\.?\s*)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", value)
    return float(match[1].replace(",", "")) if match else None


async def guard_amazon(page):
    text = (await page.locator("body").inner_text())[:12000].lower()
    if any(
        phrase in text
        for phrase in (
            "enter the characters you see",
            "type the characters you see",
            "not a robot",
            "validatecaptcha",
        )
    ):
        raise ToolFailure(
            FailureClass.AUTH,
            "Amazon needs a human verification. Complete it in the Jarvis browser, then retry.",
        )
    if "/ap/signin" in page.url:
        raise ToolFailure(FailureClass.AUTH, "Sign in to Amazon in the Jarvis browser, then retry.")


async def amazon_search_snapshot(page) -> list[dict]:
    cards = page.locator('[data-component-type="s-search-result"][data-asin]')
    items = await cards.evaluate_all("""cards =>
      cards.map(card => ({
        asin: card.getAttribute('data-asin'),
        title: card.querySelector('h2')?.innerText?.trim() || '',
        price_text: card.querySelector('.a-price .a-offscreen')?.textContent?.trim() || '',
      })).filter(item => item.asin && item.title)""")
    # Sponsored and organic placements often repeat the same product.
    products = {}
    for item in items:
        if ASIN_PATTERN.fullmatch(item["asin"]):
            products.setdefault(item["asin"], item)
    return list(products.values())[:12]


async def amazon_product_snapshot(page) -> dict:
    data = await page.evaluate("""() => {
      const text = sel => document.querySelector(sel)?.textContent?.trim() || '';
      const asin = value => (value || '').match(/(?:^|[,/])([A-Z0-9]{10})(?:$|[/?])/i)?.[1]
        ?.toUpperCase() || '';
      const options = kind => Array.from(
        document.querySelectorAll('#variation_' + kind + '_name li'))
        .map(li => ({label: li.getAttribute('title')?.replace(/^Click to select /, '')
          || li.innerText.trim(),
          asin: asin(li.getAttribute('data-defaultasin') || li.getAttribute('data-asin')
            || li.querySelector('a')?.getAttribute('href'))}));
      const dropdownOptions = kind => Array.from(document.querySelectorAll(
        '#native_dropdown_selected_' + kind + '_name option'))
        .filter(o => o.value !== '-1' && !o.disabled)
        .map(o => ({label:o.textContent.trim(), asin:asin(o.value)}));
      const sizes = dropdownOptions('size');
      const colors = dropdownOptions('color');
      return {asin: document.querySelector('#ASIN')?.value || '', title:text('#productTitle'),
        price_text:text('#corePrice_feature_div .a-price .a-offscreen')
          || text('#corePriceDisplay_desktop_feature_div .a-price .a-offscreen')
          || text('.priceToPay .a-offscreen') || text('#priceblock_dealprice')
          || text('#priceblock_ourprice'),
        size: text('#variation_size_name .selection')
          || text('#native_dropdown_selected_size_name option:checked'),
        color:text('#variation_color_name .selection')
          || text('#native_dropdown_selected_color_name option:checked'),
        sizes:sizes.length ? sizes : options('size'),
        colors:colors.length ? colors : options('color'), can_add:!!document.querySelector(
          '#add-to-cart-button:not([disabled]), #add-to-cart-button-ubb:not([disabled])')};
    }""")
    data["price"] = price_number(data.pop("price_text"))
    data["currency"] = "INR"
    return data


async def select_option(page, kind: str, wanted: str):
    if not wanted:
        return
    wanted = wanted.strip()
    expected_asin = ""
    dropdown = page.locator(f"#native_dropdown_selected_{kind}_name")
    if await dropdown.count():
        labels = await dropdown.locator("option").all_text_contents()
        exact = next(
            (label for label in labels if label.strip().casefold() == wanted.casefold()), None
        )
        if exact is None:
            raise ToolFailure(
                FailureClass.INPUT, f"Requested {kind} is not available. Choose a listed option."
            )
        option = dropdown.locator("option").filter(
            has_text=re.compile(r"^" + re.escape(exact) + r"$")
        )
        if await option.is_disabled():
            raise ToolFailure(FailureClass.INPUT, f"Requested {kind} is unavailable.")
        value = await option.get_attribute("value") or ""
        expected_asin = next(
            (part for part in value.split(",") if ASIN_PATTERN.fullmatch(part)), ""
        )
        # Amazon decorates a hidden native select; selecting it still dispatches
        # the site's input/change handlers, including variant navigation.
        await dropdown.select_option(label=exact, force=True)
    else:
        options = page.locator(f"#variation_{kind}_name li")
        found = None
        for index in range(await options.count()):
            item = options.nth(index)
            label = (
                ((await item.get_attribute("title")) or await item.inner_text())
                .replace("Click to select ", "")
                .strip()
            )
            if label.casefold() == wanted.casefold():
                found = item
                break
        if found is None:
            current = await first_text(page, f"#variation_{kind}_name .selection")
            if current.casefold() == wanted.casefold():
                return
            raise ToolFailure(
                FailureClass.INPUT, f"Cannot safely select this {kind}. Choose a listed option."
            )
        expected_asin = (
            await found.get_attribute("data-defaultasin")
            or await found.get_attribute("data-asin")
            or ""
        )
        if not ASIN_PATTERN.fullmatch(expected_asin):
            expected_asin = ""
        await found.click()
    await page.wait_for_function(
        """({kind, wanted, asin}) => {
          const normalize = value => (value || '').trim().toLocaleLowerCase();
          const selected = document.querySelector('#variation_' + kind + '_name .selection')
            ?.textContent || document.querySelector(
              '#native_dropdown_selected_' + kind + '_name option:checked')?.textContent;
          return normalize(selected) === normalize(wanted)
            && (!asin || document.querySelector('#ASIN')?.value === asin)
            && !document.querySelector('[id*=twister][aria-busy="true"]');
        }""",
        arg={"kind": kind, "wanted": wanted, "asin": expected_asin},
        timeout=10000,
    )
    # Read the actual variant repeatedly; an immediately updated label alone
    # does not mean that its price and child ASIN have finished updating.
    previous = None
    stable_since = monotonic()
    deadline = monotonic() + 8
    while monotonic() < deadline:
        current = await amazon_product_snapshot(page)
        if current != previous:
            previous = current
            stable_since = monotonic()
        elif monotonic() - stable_since >= 0.8:
            return
        await asyncio.sleep(0.2)
    raise ToolFailure(FailureClass.TRANSIENT, "Amazon variant is still updating. Try again.")


async def cart_snapshot(page, asin: str) -> dict | None:
    lines = page.locator(f'#sc-active-cart .sc-list-item[data-asin="{asin}"]')
    if await lines.count() != 1:
        return None
    line = lines.first
    quantity = await line.get_attribute("data-quantity")
    if quantity is None:
        select = line.locator('select[name="quantity"]')
        quantity = await select.input_value() if await select.count() else None
    if quantity is None:
        numeric_input = line.locator('input[name="quantity"], input[role="spinbutton"]')
        quantity = await numeric_input.first.input_value() if await numeric_input.count() else None
    if quantity is None:
        quantity = await first_text(
            line, '[data-a-selector="value"], .sc-quantity-stepper [role="spinbutton"]'
        )
    quantity = quantity.strip() if quantity else None
    price = price_number(await first_text(line, ".sc-product-price, .a-price .a-offscreen"))
    if not quantity or not quantity.isdigit() or price is None:
        return None
    return {
        "asin": asin,
        "quantity": int(quantity),
        "unit_price": price,
        "currency": "INR",
        "cart_url": "https://www.amazon.in/gp/cart/view.html",
    }


async def wait_product(page):
    await page.locator(PRODUCT_READY).first.wait_for(state="attached", timeout=12000)
    await guard_amazon(page)


async def open_cart(page):
    await page.goto(
        "https://www.amazon.in/gp/cart/view.html", wait_until="domcontentloaded", timeout=15000
    )
    await guard_amazon(page)
    await page.locator(CART_READY).first.wait_for(state="attached", timeout=8000)


class AmazonTool(BrowserTool):
    @property
    def timeout(self) -> float:
        # The engine includes independent readback in this same deadline.
        return 150 if self.name == "amazon.add_to_cart" else 75

    async def run(self, args, key, store):
        browser = session(store)
        task_id = key.split(":")[0]
        async with browser.lock:
            page = await browser.page("amazon-" + task_id)
            try:
                if self.name == "amazon.search":
                    await page.goto(
                        "https://www.amazon.in/s?"
                        + urlencode({"k": args["query"], "i": "apparel"}),
                        wait_until="domcontentloaded",
                        timeout=25000,
                    )
                    await guard_amazon(page)
                    await page.locator(
                        '[data-component-type="s-search-result"][data-asin] h2, .s-no-results'
                    ).first.wait_for(state="attached", timeout=12000)
                    products = await amazon_search_snapshot(page)
                    if not products:
                        raise ToolFailure(
                            FailureClass.INPUT,
                            (
                                "No readable Amazon products found. Refine the query or check the "
                                "browser."
                            ),
                        )
                    for product in products:
                        product["price"] = price_number(product.pop("price_text"))
                        product["url"] = "https://www.amazon.in/dp/" + product["asin"]
                    return {
                        "id": key,
                        "query": args["query"],
                        "products": products,
                        "currency": "INR",
                    }
                await page.goto(
                    "https://www.amazon.in/dp/" + args["asin"],
                    wait_until="domcontentloaded",
                    timeout=25000,
                )
                await wait_product(page)
                await select_option(page, "color", args.get("color", ""))
                await select_option(page, "size", args.get("size", ""))
                product = await amazon_product_snapshot(page)
                if not product["asin"] or not product["title"]:
                    raise ToolFailure(
                        FailureClass.INPUT, "Amazon product details could not be read reliably."
                    )
                if any(
                    args.get(kind)
                    and product[kind].strip().casefold() != args[kind].strip().casefold()
                    for kind in ("size", "color")
                ):
                    raise ToolFailure(
                        FailureClass.INPUT,
                        "Amazon did not retain the requested size/color. "
                        "Inspect the variant again.",
                    )
                if self.name == "amazon.inspect":
                    await store.db.browser_observations.update_one(
                        {"_id": task_id + ":" + product["asin"]}, {"$set": product}, upsert=True
                    )
                    return {"id": key, **product}
                if (
                    product["asin"] != args["asin"]
                    or product["title"] != args["title"]
                    or product["price"] is None
                    or product["price"] != args["unit_price"]
                    or product["price"] > args["max_price"]
                    or (product["sizes"] and not args["size"])
                    or (args["size"] and product["size"].casefold() != args["size"].casefold())
                    or (args["color"] and product["color"].casefold() != args["color"].casefold())
                ):
                    raise ToolFailure(
                        FailureClass.INPUT,
                        (
                            "Product, price, size, or color changed. Inspect it again before "
                            "approving."
                        ),
                    )
                seen = await store.db.browser_observations.find_one(
                    {"_id": task_id + ":" + args["asin"]}
                )
                if (
                    not seen
                    or seen["price"] != args["unit_price"]
                    or seen["title"] != args["title"]
                    or any(seen.get(kind, "") != args.get(kind, "") for kind in ("size", "color"))
                ):
                    raise ToolFailure(
                        FailureClass.INPUT,
                        "Inspect this exact variant before adding it to the cart.",
                    )
                cart = await browser.page("cart-" + task_id)
                await open_cart(cart)
                existing = await cart_snapshot(cart, args["asin"])
                if existing:
                    if (
                        existing["quantity"] >= 1
                        and existing["unit_price"] == args["unit_price"]
                        and existing["unit_price"] <= args["max_price"]
                    ):
                        return {"id": key, **existing, "already_present": True}
                    raise ToolFailure(
                        FailureClass.INPUT,
                        "This variant is already in the cart at a different price or quantity. "
                        "Review the cart before continuing; no duplicate was added.",
                    )
                if await cart.locator(
                    f'#sc-active-cart .sc-list-item[data-asin="{args["asin"]}"]'
                ).count():
                    raise ToolFailure(
                        FailureClass.INPUT,
                        "This variant is already in the cart but its quantity or price is "
                        "unreadable. Review the cart; no duplicate was added.",
                    )
                if not product["can_add"]:
                    raise ToolFailure(
                        FailureClass.INPUT,
                        "This variant is unavailable to add. Choose an available size/color.",
                    )

                # Validate the live price again after inspecting the cart. The
                # page can refresh the offer while the other tab is loading.
                current = await amazon_product_snapshot(page)
                if any(
                    current.get(field) != product.get(field)
                    for field in ("asin", "title", "price", "size", "color")
                ):
                    raise ToolFailure(
                        FailureClass.INPUT,
                        "Amazon changed the offer while checking the cart. Inspect it again.",
                    )

                try:
                    await store.db.browser_dispatches.insert_one(
                        {
                            "_id": key,
                            "provider": "amazon",
                            "at": now(),
                            "args": args,
                            "state": "submitting",
                        }
                    )
                except DuplicateKeyError as exc:
                    raise ToolFailure(
                        FailureClass.UNRECOVERABLE,
                        (
                            "An earlier cart addition is uncertain. Check the cart; no "
                            "duplicate add will run."
                        ),
                        False,
                    ) from exc
                await page.locator(
                    "#add-to-cart-button:visible, #add-to-cart-button-ubb:visible"
                ).first.click(timeout=10000)
                await store.db.browser_dispatches.update_one(
                    {"_id": key}, {"$set": {"state": "submitted"}}
                )
                return {
                    "id": key,
                    "asin": args["asin"],
                    "cart_url": "https://www.amazon.in/gp/cart/view.html",
                }
            except (BrowserError, BrowserTimeout) as exc:
                dispatch = await store.db.browser_dispatches.find_one({"_id": key})
                raise ToolFailure(
                    FailureClass.TRANSIENT,
                    "Amazon page did not respond as expected. Check the Jarvis browser.",
                    retry_allowed=self.retry_safe or dispatch is None,
                ) from exc

    async def _verify(self, args, result, store):
        browser = session(store)
        async with browser.lock:
            if self.name == "amazon.add_to_cart":
                page = await browser.page("cart-" + result["id"].split(":")[0])
                # Cart writes can finish after the click returns. Re-read with
                # bounded backoff; never repeat the add action to resolve a race.
                for attempt in range(3):
                    if attempt:
                        await asyncio.sleep(attempt)
                    await open_cart(page)
                    line = await cart_snapshot(page, args["asin"])
                    if line:
                        if (
                            line["quantity"] >= 1
                            and line["unit_price"] == args["unit_price"]
                            and line["unit_price"] <= args["max_price"]
                        ):
                            return True
                        break
                raise ToolFailure(
                    FailureClass.LOGIC,
                    "The exact variant and approved price could not be verified in the "
                    "active cart. Inspect the cart, then use Verify again; no second add will run.",
                    False,
                )
            # Read operations re-read the rendered source, independently of the returned object.
            page = browser.pages.get("amazon-" + result["id"].split(":")[0])
            pages = [page] if page and not page.is_closed() else []
            for page in pages:
                if self.name == "amazon.search" and "/s?" in page.url and result.get("products"):
                    current = await amazon_search_snapshot(page)
                    if {item["asin"] for item in result["products"]} <= {
                        item["asin"] for item in current
                    }:
                        return True
                if self.name == "amazon.inspect" and "/dp/" in page.url:
                    current = await amazon_product_snapshot(page)
                    if all(
                        current.get(field) == result.get(field)
                        for field in ("asin", "title", "price", "size", "color")
                    ):
                        return True
            return False


AMAZON_TOOLS = {
    "amazon.search": AmazonTool(
        "amazon.search",
        "amazon",
        (
            "Search Amazon India clothing by query. Returns observed products, "
            "ASINs and INR prices; inspect before adding to cart."
        ),
        SearchArgs,
        "read",
        mode="live",
    ),
    "amazon.inspect": AmazonTool(
        "amazon.inspect",
        "amazon",
        (
            "Inspect an observed Amazon ASIN. Returns exact title, price, "
            "selected size/color and available options. Optional size/color "
            "must exactly match displayed options. Variant ASIN may change "
            "after selection."
        ),
        InspectArgs,
        "read",
        mode="live",
    ),
    "amazon.add_to_cart": AmazonTool(
        "amazon.add_to_cart",
        "amazon",
        (
            "Ensure ONE unit of a previously inspected exact variant is in the "
            "Amazon India cart. Requires observed title, ASIN, size/color, "
            "current unit_price and approved max_price in INR. Does not "
            "increase an existing quantity, pay, or checkout. Requires "
            "approval and independent cart read-back."
        ),
        CartArgs,
        "write_irreversible",
        retry_safe=False,
        mode="live",
    ),
}
