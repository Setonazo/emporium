import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DUFRY_EMAIL = os.getenv("DUFRY_EMAIL")
DUFRY_PASSWORD = os.getenv("DUFRY_PASSWORD")
COOKIES_FILE = Path(os.getenv("COOKIES_FILE", "/tmp/dufry_session.json"))

SSO_LOGIN_URL = (
    "https://sso.clubavolta.com/login"
    "?response_type=code"
    "&redirect_uri=https://esfnf.emporium.dufry.com/sso/redirect"
    "&client_id=jozu6c3hqypeq"
    "&scope=openid+email"
    "&privacy_url=null"
    "&lang=es"
)

@dataclass
class StockResult:
    in_stock: Optional[bool]
    name: Optional[str]
    error: Optional[str] = field(default=None)

_NAME_SELECTORS = [
    "h1.cx-heading", "h1.product-name", ".pdp-summary__name",
    ".product-details__name", ".product__title", "h1",
]
_ADD_TO_CART = ", ".join([
    "cx-add-to-cart button", "button.btn-add-to-cart",
    "button[data-testid*='add-to-cart']", "button[data-action*='addToCart']",
    "button[id*='addToCart']", ".add-to-cart button", "button.addToCart",
])
_OOS_SELECTORS = [
    ".cx-out-of-stock", ".out-of-stock", "[class*='outOfStock']",
    "[class*='out-of-stock']", "[data-stock='false']",
]
_OOS_TEXT = [
    "out of stock", "sin stock", "agotado", "no disponible",
    "sold out", "sin existencias", "not available",
]
_IN_STOCK_TEXT = [
    "in stock", "en stock", "add to cart", "anadir al carrito",
    "agregar al carrito", "comprar ahora", "buy now",
]


async def _save_cookies(ctx):
    cookies = await ctx.cookies()
    COOKIES_FILE.write_text(json.dumps(cookies))
    logger.info("Session saved (%d cookies)", len(cookies))


async def _load_cookies(ctx) -> bool:
    if not COOKIES_FILE.exists():
        return False
    try:
        cookies = json.loads(COOKIES_FILE.read_text())
        await ctx.add_cookies(cookies)
        logger.info("Session loaded (%d cookies)", len(cookies))
        return True
    except Exception:
        return False


def _needs_login(url: str) -> bool:
    return any(kw in url for kw in (
        "sso.clubavolta.com", "login", "signin", "sign-in",
    ))


async def _fill_login_form(page) -> bool:
    """Fill and submit the login form on the current page."""
    if not DUFRY_EMAIL or not DUFRY_PASSWORD:
        logger.error("DUFRY_EMAIL / DUFRY_PASSWORD not set in environment")
        return False

    try:
        logger.info("Filling login form at: %s", page.url)

        # Wait for the form to appear
        await page.wait_for_selector("input[type='password']", timeout=10_000)

        # Email/username — try several selectors
        email_el = None
        for sel in (
            "input[type='email']",
            "input[name='email']", "input[name='username']",
            "input[name='identifier']", "input[id='email']",
            "input[id='username']", "input[type='text']",
        ):
            email_el = await page.query_selector(sel)
            if email_el:
                logger.info("Email field found: %s", sel)
                break

        if not email_el:
            logger.error("Email field not found. Page title: %s", await page.title())
            return False

        await email_el.fill(DUFRY_EMAIL)

        # Password
        pass_el = await page.query_selector("input[type='password']")
        if not pass_el:
            logger.error("Password field not found")
            return False

        await pass_el.fill(DUFRY_PASSWORD)

        # Submit
        submit_el = await page.query_selector("button[type='submit']")
        if submit_el:
            await submit_el.click()
        else:
            await pass_el.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=20_000)
        logger.info("After login submit, URL: %s", page.url)

        # Still on login page = wrong credentials
        if _needs_login(page.url):
            logger.warning("Still on login page after submit — check credentials")
            return False

        return True

    except Exception as exc:
        logger.error("Login error: %s", exc)
        return False


async def _login(page, product_url: str) -> bool:
    """Navigate to SSO login if needed and authenticate."""
    # Go directly to SSO login URL (preserves the redirect_uri back to Dufry)
    from playwright.async_api import TimeoutError as PWTimeout
    try:
        await page.goto(SSO_LOGIN_URL, wait_until="networkidle", timeout=30_000)
    except PWTimeout:
        pass

    return await _fill_login_form(page)


async def check_stock(url: str, custom_selector: str = None) -> StockResult:
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        return StockResult(in_stock=None, name=None,
            error="Playwright no instalado.")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
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

            # Load the product page
            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
            except PWTimeout:
                logger.warning("networkidle timeout — continuing")
            except Exception as exc:
                await browser.close()
                return StockResult(in_stock=None, name=None,
                    error=f"Error de red: {str(exc)[:120]}")

            logger.info("Page loaded. URL: %s | Title: %s", page.url, await page.title())

            # Login if redirected to SSO or login page
            if _needs_login(page.url):
                logger.info("Login required (URL: %s)", page.url)
                ok = await _login(page, url)
                if not ok:
                    await browser.close()
                    return StockResult(in_stock=None, name=None,
                        error="Login fallido. Revisa DUFRY_EMAIL y DUFRY_PASSWORD y los logs de Railway.")
                await _save_cookies(ctx)
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30_000)
                    logger.info("Back on product page: %s", page.url)
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
            logger.info("Product name: %s", name)

            # Custom selector
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

            # Explicit OOS elements
            for sel in _OOS_SELECTORS:
                if await page.query_selector(sel):
                    logger.info("OOS selector matched: %s", sel)
                    await browser.close()
                    return StockResult(in_stock=False, name=name)

            # Add-to-cart button state
            btn = await page.query_selector(_ADD_TO_CART)
            if btn is not None:
                disabled = await btn.get_attribute("disabled")
                cls = (await btn.get_attribute("class")) or ""
                in_stock = disabled is None and "disabled" not in cls
                logger.info("Cart button found — in_stock=%s cls=%s", in_stock, cls)
                await browser.close()
                return StockResult(in_stock=in_stock, name=name)

            # Text fallback
            try:
                body = (await page.inner_text("body")).lower()
            except Exception:
                body = ""

            for kw in _OOS_TEXT:
                if kw in body:
                    await browser.close()
                    return StockResult(in_stock=False, name=name)
            for kw in _IN_STOCK_TEXT:
                if kw in body:
                    await browser.close()
                    return StockResult(in_stock=True, name=name)

            logger.warning("Stock undetected. Title=%s | Body[:300]=%s",
                           await page.title(), body[:300])
            await browser.close()
            return StockResult(in_stock=None, name=name,
                error="No se pudo detectar stock automaticamente")

    except Exception as exc:
        logger.exception("Unexpected error for %s", url)
        return StockResult(in_stock=None, name=None,
            error=f"Error inesperado: {str(exc)[:120]}")
