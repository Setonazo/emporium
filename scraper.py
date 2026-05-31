import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Only one Chromium instance at a time — prevents EAGAIN on resource-limited hosts
_browser_semaphore = asyncio.Semaphore(1)

logger = logging.getLogger(__name__)

DUFRY_EMAIL = os.getenv("DUFRY_EMAIL")
DUFRY_PASSWORD = os.getenv("DUFRY_PASSWORD")
COOKIES_FILE = Path(os.getenv("COOKIES_FILE", "/tmp/dufry_session.json"))

SSO_LOGIN_URL = (
    "<https://sso.clubavolta.com/login>"
    "?response_type=code"
    "&redirect_uri=<https://esfnf.emporium.dufry.com/sso/redirect>"
    "&client_id=jozu6c3hqypeq"
    "&scope=openid+email"
    "&privacy_url=null"
    "&lang=es"
)

# Button texts that mean "out of stock — notify me"
_NOTIFY_TEXTS = ["notif", "avís", "avisame", "avísame", "alert", "back in stock", "avisa"]
# Button texts that mean "add to cart"
_ADD_TEXTS = ["añadir", "agregar", "add to cart", "add to bag", "comprar"]

# Selectors for the cart confirmation dialog (Spartacus)
_CART_CONFIRMED_SELECTORS = [
    "cx-added-to-cart-dialog",
    "[class*='AddedToCart']",
    "[class*='added-to-cart']",
    ".cx-dialog-title",
]
# Text in body after successful add-to-cart (Dufry's mini-cart popup)
_CART_SUCCESS_TEXTS = [
    "artículo añadido a tu cesta",
    "añadido a tu cesta",
    "artículo añadido",
    "item added",
    "added to your bag",
    "added to cart",
    "producto añadido",
]
# Error texts that appear after a failed add-to-cart
_OOS_AFTER_CLICK = [
    "out of stock", "sin stock", "agotado", "no hay stock",
    "not available", "no disponible", "unavailable",
]
# Page-level OOS indicators (appear before any clicking)
_OOS_PAGE_TEXTS = [
    "producto agotado",
    "este producto no está disponible",
    "artículo agotado",
    "no está disponible actualmente",
    "out of stock",
]


@dataclass
class StockResult:
    in_stock: Optional[bool]
    name: Optional[str]
    error: Optional[str] = field(default=None)


_NAME_SELECTORS = [
    "<h1.cx>-heading", "h1.product-name", ".pdp-summary__name",
    ".product-details__name", ".product__title", "h1",
]


# ── Session helpers ───────────────────────────────────────────────────────────

async def _save_cookies(ctx):
    cookies = await ctx.cookies()
    COOKIES_FILE.write_text(json.dumps(cookies))
    <logger.info>("Session saved (%d cookies)", len(cookies))


async def _load_cookies(ctx) -> bool:
    if not COOKIES_FILE.exists():
        return False
    try:
        cookies = json.loads(COOKIES_FILE.read_text())
        await ctx.add_cookies(cookies)
        <logger.info>("Session loaded (%d cookies)", len(cookies))
        return True
    except Exception:
        return False


def _needs_login(url: str) -> bool:
    from urllib.parse import urlparse
    p = urlparse(url)
    base = p.netloc + p.path
    return any(kw in base for kw in ("<sso.clubavolta.com>", "/login", "/signin"))


async def _first_visible(page, selectors: list) -> Optional[object]:
    for sel in selectors:
        els = await page.query_selector_all(sel)
        for el in els:
            if await el.is_visible():
                return el
    return None


# ── Cookie banner ─────────────────────────────────────────────────────────────

async def _dismiss_cookie_banner(page):
    try:
        dismissed = await page.evaluate("""() => {
            const uc = document.querySelector('#usercentrics-root');
            if (uc && uc.shadowRoot) {
                const btn = uc.shadowRoot.querySelector(
                    '[data-testid="uc-accept-all-button"], button[class*="accept"]'
                );
                if (btn) { btn.click(); return true; }
            }
            return false;
        }""")
        if dismissed:
            <logger.info>("Cookie banner dismissed")
            await page.wait_for_timeout(500)
            return
    except Exception:
        pass
    try:
        await page.evaluate("""() => {
            const el = document.querySelector('#usercentrics-root, #cookie-banner, .cookie-consent');
            if (el) el.style.display = 'none';
        }""")
    except Exception:
        pass


# ── Login ─────────────────────────────────────────────────────────────────────

async def _do_login(page) -> bool:
    if not DUFRY_EMAIL or not DUFRY_PASSWORD:
        logger.error("DUFRY_EMAIL / DUFRY_PASSWORD not configured")
        return False

    try:
        await page.goto(SSO_LOGIN_URL, wait_until="networkidle", timeout=30_000)
        <logger.info>("SSO page loaded. Title: %s", await page.title())
        await _dismiss_cookie_banner(page)

        email_el = await _first_visible(page, [
            "input[type='email']", "input[name='email']",
            "input[name='username']", "input[name='identifier']",
            "input[type='text']",
        ])
        if not email_el:
            logger.error("No visible email field on SSO page")
            return False

        await email_el.fill(DUFRY_EMAIL)
        <logger.info>("Email filled")

        submit_btn = await _first_visible(page, ["button[type='submit']"])
        if submit_btn:
            await submit_btn.click()
            <logger.info>("Clicked submit (two-step)")
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            await _dismiss_cookie_banner(page)

        try:
            await page.wait_for_selector("input[type='password']", state="visible", timeout=10_000)
        except Exception:
            logger.warning("Password field slow to appear")

        pass_el = await _first_visible(page, ["input[type='password']"])
        if not pass_el:
            logger.error("No visible password field")
            return False

        await pass_el.fill(DUFRY_PASSWORD)
        <logger.info>("Password filled")

        await _dismiss_cookie_banner(page)
        submit_btn = await _first_visible(page, ["button[type='submit']"])
        if submit_btn:
            await submit_btn.click()
        else:
            await pass_el.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=20_000)
        <logger.info>("Post-login URL: %s", page.url)

        if _needs_login(page.url):
            logger.warning("Still on login page after submit")
            return False

        <logger.info>("Login successful")
        return True

    except Exception as exc:
        logger.error("Login failed: %s", exc)
        return False


# ── Cart helpers ──────────────────────────────────────────────────────────────

async def _remove_from_cart(page, base_url: str):
    """Navigate to cart page and remove all items to clean up."""
    try:
        cart_url = base_url.rstrip("/") + "/es/cart"
        await page.goto(cart_url, wait_until="networkidle", timeout=20_000)
        await _dismiss_cookie_banner(page)

        removed = 0
        for _ in range(10):  # remove up to 10 items
            btn = await _first_visible(page, [
                "button[aria-label*='Remove']",
                "button[aria-label*='Eliminar']",
                ".cx-remove-btn",
                "[class*='remove-item']",
                "button[class*='Remove']",
            ])
            if not btn:
                break
            await btn.click()
            await page.wait_for_timeout(800)
            removed += 1

        <logger.info>("Removed %d item(s) from cart", removed)
    except Exception as exc:
        logger.warning("Cart cleanup failed (non-critical): %s", exc)


# ── Stock verification by actually clicking ───────────────────────────────────

def _base_url(product_url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(product_url)
    return f"{p.scheme}://{p.netloc}"


_ACTION_BUTTON_KEYWORDS = ["añadir", "agregar", "add to cart", "add to bag", "comprar",
                           "avís", "avisame", "avísame", "notif", "avisa"]


async def _verify_stock_by_clicking(page, product_url: str) -> Optional[bool]:
    """
    Finds the action button and checks whether it is 'add to cart' or
    'notify me'. If it is 'add to cart', clicks it and checks whether
    the item was actually added (verifying real availability).
    Cleans up the cart afterwards.
    """
    await _dismiss_cookie_banner(page)

    # Wait until the SPA renders the actual product action button (not nav buttons)
    <logger.info>("Waiting for product action button to render...")
    try:
        await page.wait_for_function(
            """(kws) => Array.from(document.querySelectorAll('button')).some(
                btn => kws.some(kw => (btn.innerText || '').toLowerCase().includes(kw))
            )""",
            _ACTION_BUTTON_KEYWORDS,
            timeout=15_000,
        )
        <logger.info>("Action button appeared in DOM")
    except Exception:
        logger.warning("Action button not found within 15s — checking OOS text")

    # OOS check via page text (covers "producto agotado" page state)
    try:
        body_pre = (await page.inner_text("body")).lower()
    except Exception:
        body_pre = ""
    for kw in _OOS_PAGE_TEXTS:
        if kw in body_pre:
            <logger.info>("Page-level OOS indicator found: '%s' — out of stock", kw)
            return False

    buttons = await page.query_selector_all("button")
    add_btn = None
    notify_btn = None
    visible_texts = []

    for btn in buttons:
        if not await btn.is_visible():
            continue
        text = (await btn.inner_text()).lower().strip()
        cls = ((await btn.get_attribute("class")) or "").lower()
        if text:
            visible_texts.append(repr(text[:40]))

        if any(kw in text for kw in _NOTIFY_TEXTS):
            notify_btn = btn
            <logger.info>("Notify-me button found: '%s'", text[:60])
        elif any(kw in text or kw in cls for kw in _ADD_TEXTS):
            add_btn = btn
            <logger.info>("Add-to-cart button found: '%s'", text[:60])

    <logger.info>("Visible buttons on page: %s", visible_texts[:15])

    # Notify-me present without add-to-cart → definitely out of stock
    if notify_btn and not add_btn:
        <logger.info>("Only notify-me button visible — out of stock")
        return False

    if not add_btn:
        logger.warning("No action button found")
        return None

    # Check if the add-to-cart button is disabled
    disabled = await add_btn.get_attribute("disabled")
    cls = (await add_btn.get_attribute("class")) or ""
    if disabled is not None or "disabled" in cls:
        <logger.info>("Add-to-cart button is disabled — out of stock")
        return False

    # Click and wait for the cart popup to actually appear in the DOM
    <logger.info>("Clicking add-to-cart to verify actual stock...")
    try:
        await add_btn.click()
    except Exception as exc:
        logger.warning("Click failed: %s", exc)
        return None

    <logger.info>("Waiting for cart result popup...")
    try:
        await page.wait_for_function(
            """(data) => {
                const body = (document.body.innerText || '').toLowerCase();
                return data.success.some(k => body.includes(k))
                    || data.oos.some(k => body.includes(k))
                    || data.sel.some(s => !!document.querySelector(s));
            }""",
            {"success": _CART_SUCCESS_TEXTS, "oos": _OOS_AFTER_CLICK, "sel": _CART_CONFIRMED_SELECTORS},
            timeout=15_000,
        )
        <logger.info>("Cart result appeared in DOM")
    except Exception:
        logger.warning("Cart result did not appear within 15s")
        await page.wait_for_timeout(2_000)

    await _dismiss_cookie_banner(page)

    # Check for cart confirmation dialog (Spartacus component selectors)
    for sel in _CART_CONFIRMED_SELECTORS:
        if await page.query_selector(sel):
            <logger.info>("Cart confirmation dialog found (%s) — IN STOCK", sel)
            await _remove_from_cart(page, _base_url(product_url))
            return True

    # Check body text for Dufry's success message
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""

    for kw in _CART_SUCCESS_TEXTS:
        if kw in body:
            <logger.info>("Cart success text found: '%s' — IN STOCK", kw)
            await _remove_from_cart(page, _base_url(product_url))
            return True

    for kw in _OOS_AFTER_CLICK:
        if kw in body:
            <logger.info>("OOS error after clicking add-to-cart: '%s'", kw)
            return False

    logger.warning("Add-to-cart result unclear after click")
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

async def check_stock(url: str, custom_selector: str = None) -> StockResult:
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        return StockResult(in_stock=None, name=None, error="Playwright no instalado.")

    async with _browser_semaphore:
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--no-zygote",
                        "--single-process",
                    ],
                )
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="es-ES",
                    timezone_id="Europe/Madrid",
                )
                page = await ctx.new_page()
                await _load_cookies(ctx)

                try:
                    await page.goto(url, wait_until="networkidle", timeout=30_000)
                except PWTimeout:
                    logger.warning("networkidle timeout — continuing")
                except Exception as exc:
                    await browser.close()
                    return StockResult(in_stock=None, name=None,
                        error=f"Error de red: {str(exc)[:120]}")

                <logger.info>("Page loaded. URL: %s | Title: %s", page.url, await page.title())

                # Login if needed
                if _needs_login(page.url):
                    <logger.info>("Login required")
                    ok = await _do_login(page)
                    if not ok:
                        await browser.close()
                        return StockResult(in_stock=None, name=None,
                            error="Login fallido. Revisa DUFRY_EMAIL y DUFRY_PASSWORD.")
                    await _save_cookies(ctx)
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=30_000)
                        <logger.info>("Back on product page: %s", page.url)
                    except PWTimeout:
                        pass

                # Product name
                name: Optional[str] = None
                for sel in _NAME_SELECTORS:
                    el = await page.query_selector(sel)
                    if el:
                        text = (await el.inner_text()).strip()[:120]
                        if text:
                            name = text
                            break
                <logger.info>("Product name: %s", name)

                # Custom selector (manual override)
                if custom_selector:
                    el = await page.query_selector(custom_selector)
                    if el is not None:
                        disabled = await el.get_attribute("disabled")
                        cls = (await el.get_attribute("class")) or ""
                        await browser.close()
                        return StockResult(
                            in_stock=disabled is None and "disabled" not in cls,
                            name=name,
                        )

                # Primary: verify by actually clicking add-to-cart
                in_stock = await _verify_stock_by_clicking(page, url)

                await browser.close()

                if in_stock is not None:
                    return StockResult(in_stock=in_stock, name=name)

                return StockResult(in_stock=None, name=name,
                    error="No se pudo verificar el stock (botón de acción no encontrado)")

        except Exception as exc:
            logger.exception("Unexpected error for %s", url)
            return StockResult(in_stock=None, name=None,
                error=f"Error inesperado: {str(exc)[:120]}")
