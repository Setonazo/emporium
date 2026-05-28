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

# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class StockResult:
    in_stock: Optional[bool]
    name: Optional[str]
    error: Optional[str] = field(default=None)


# ── CSS selectors ─────────────────────────────────────────────────────────────

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


# ── Session helpers ───────────────────────────────────────────────────────────

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


async def _has_login_form(page) -> bool:
    """Returns True if the current page has a password input field."""
    el = await page.query_selector("input[type='password']")
    return el is not None


# ── Login ─────────────────────────────────────────────────────────────────────

async def _do_login(page, target_url: str) -> bool:
    """
    Fills the login form on the current page (if present),
    or navigates to the login page first.
    """
    if not DUFRY_EMAIL or not DUFRY_PASSWORD:
        logger.warning("DUFRY_EMAIL / DUFRY_PASSWORD not configured")
        return False

    try:
        # If there's no login form on current page, go to the login page
        if not await _has_login_form(page):
            base = target_url.split("/es/")[0] if "/es/" in target_url else "https://esfnf.emporium.dufry.com"
            for login_path in ("/es/login", "/login", "/es/my-account/login"):
                try:
                    await page.goto(f"{base}{login_path}", wait_until="networkidle", timeout=20_000)
                    if await _has_login_form(page):
                        logger.info("Login form found at %s%s", base, login_path)
                        break
                except Exception:
                    continue
            else:
                logger.error("Could not find login form on any known URL")
                return False

        logger.info("Filling login form on: %s", page.url)

        # Fill email — try by type first (most reliable), then by common names
        email_el = await page.query_selector("input[type='email']")
        if not email_el:
            for sel in ("input[formcontrolname='userId']", "input[formcontrolname='email']",
                        "input[name='email']", "input[name='j_username']", "#email", "#userId"):
                email_el = await page.query_selector(sel)
                if email_el:
                    break
        if not email_el:
            # Last resort: first visible text input
            email_el = await page.query_selector("input[type='text']")

        if not email_el:
            logger.error("Email/username field not found")
            return False

        await email_el.fill(DUFRY_EMAIL)
        logger.info("Email field filled")

        # Fill password
        pass_el = await page.query_selector("input[type='password']")
        if not pass_el:
            logger.error("Password field not found")
            return False

        await pass_el.fill(DUFRY_PASSWORD)
        logger.info("Password field filled")

        # Submit
        submit_el = await page.query_selector("button[type='submit']")
        if submit_el:
            await submit_el.click()
            logger.info("Clicked submit button")
        else:
            await pass_el.press("Enter")
            logger.info("Pressed Enter to submit")

        await page.wait_for_load_state("networkidle", timeout=20_000)
        logger.info("After login, URL is: %s", page.url)

        # Login failed if password field still visible
        if await _has_login_form(page):
            logger.warning("Password field still visible after submit — wrong credentials?")
            return False

        logger.info("Login successful")
        return True

    except Exception as exc:
        logger.error("Login error: %s", exc)
        return False


# ── Main scraper ──────────────────────────────────────────────────────────────

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

            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
            except PWTimeout:
                logger.warning("networkidle timeout for %s — continuing", url)
            except Exception as exc:
                await browser.close()
                return StockResult(in_stock=None, name=None,
                    error=f"Error de red: {str(exc)[:120]}")

            logger.info("Loaded page: %s (URL now: %s)", url, page.url)

            # Detect login requirement: login form present OR URL contains auth keywords
            needs_login = await _has_login_form(page) or any(
                kw in page.url.lower() for kw in ("login", "signin", "sign-in", "my-account/auth")
            )

            if needs_login:
                logger.info("Login required, attempting...")
                logged_in = await _do_login(page, url)
                if not logged_in:
                    await browser.close()
                    return StockResult(in_stock=None, name=None,
                        error="Login fallido. Verifica DUFRY_EMAIL y DUFRY_PASSWORD, y revisa los logs de Railway.")
                await _save_cookies(ctx)
                # Navigate back to product after login
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30_000)
                    logger.info("Returned to product page after login")
                except PWTimeout:
                    pass

            # ── Product name ─────────────────────────────────────────────────
            name: Optional[str] = None
            for sel in _NAME_SELECTORS:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()[:120]
                    if text:
                        name = text
                        break
            logger.info("Product name detected: %s", name)

            # ── Custom selector ──────────────────────────────────────────────
            if custom_selector:
                el = await page.query_selector(custom_selector)
                if el is not None:
                    disabled = await el.get_attribute("disabled")
                    cls = (await el.get_attribute("class")) or ""
                    in_stock = disabled is None and "disabled" not in cls
                    await browser.close()
                    return StockResult(in_stock=in_stock, name=name)

            # ── Explicit OOS elements ────────────────────────────────────────
            for sel in _OOS_SELECTORS:
                if await page.query_selector(sel):
                    logger.info("OOS selector matched: %s", sel)
                    await browser.close()
                    return StockResult(in_stock=False, name=name)

            # ── Add-to-cart button state ─────────────────────────────────────
            btn = await page.query_selector(_ADD_TO_CART)
            if btn is not None:
                disabled = await btn.get_attribute("disabled")
                cls = (await btn.get_attribute("class")) or ""
                in_stock = disabled is None and "disabled" not in cls
                logger.info("Add-to-cart button found, disabled=%s, classes=%s", disabled, cls)
                await browser.close()
                return StockResult(in_stock=in_stock, name=name)

            # ── Text-based fallback ──────────────────────────────────────────
            try:
                body = (await page.inner_text("body")).lower()
            except Exception:
                body = ""

            for kw in _OOS_TEXT:
                if kw in body:
                    logger.info("OOS text matched: %s", kw)
                    await browser.close()
                    return StockResult(in_stock=False, name=name)
            for kw in _IN_STOCK_TEXT:
                if kw in body:
                    logger.info("In-stock text matched: %s", kw)
                    await browser.close()
                    return StockResult(in_stock=True, name=name)

            # Log a snippet to help diagnose selector issues
            logger.warning("Could not detect stock. Page title: %s | Body snippet: %.200s",
                           await page.title(), body[:200])

            await browser.close()
            return StockResult(in_stock=None, name=name,
                error="No se pudo detectar el estado del stock automaticamente")

    except Exception as exc:
        logger.exception("Unexpected scraper error for %s", url)
        return StockResult(in_stock=None, name=None,
            error=f"Error inesperado: {str(exc)[:120]}")
