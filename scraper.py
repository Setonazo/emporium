import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class StockResult:
    in_stock: Optional[bool]
    name: Optional[str]
    error: Optional[str] = field(default=None)


_NAME_SELECTORS = [
    "h1.cx-heading",
    "h1.product-name",
    ".pdp-summary__name",
    ".product-details__name",
    ".product__title",
    "h1",
]

_ADD_TO_CART = ", ".join([
    "cx-add-to-cart button",
    "button.btn-add-to-cart",
    "button[data-testid*='add-to-cart']",
    "button[data-action*='addToCart']",
    "button[id*='addToCart']",
    ".add-to-cart button",
    "button.addToCart",
])

_OOS_SELECTORS = [
    ".cx-out-of-stock",
    ".out-of-stock",
    "[class*='outOfStock']",
    "[class*='out-of-stock']",
    "[data-stock='false']",
]

_OOS_TEXT = [
    "out of stock", "sin stock", "agotado", "no disponible",
    "sold out", "sin existencias", "not available",
]
_IN_STOCK_TEXT = [
    "in stock", "en stock", "add to cart", "añadir al carrito",
    "agregar al carrito", "comprar ahora", "buy now",
]


async def check_stock(url: str, custom_selector: str = None) -> StockResult:
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        return StockResult(
            in_stock=None,
            name=None,
            error="Playwright no instalado. Ejecuta: pip install playwright && playwright install chromium",
        )

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

            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
            except PWTimeout:
                logger.warning("networkidle timeout for %s - continuing anyway", url)
            except Exception as exc:
                await browser.close()
                return StockResult(in_stock=None, name=None, error=f"Error de red: {str(exc)[:120]}")

            name: Optional[str] = None
            for sel in _NAME_SELECTORS:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()[:120]
                    if text:
                        name = text
                        break

            if custom_selector:
                el = await page.query_selector(custom_selector)
                if el is not None:
                    disabled = await el.get_attribute("disabled")
                    cls = (await el.get_attribute("class")) or ""
                    in_stock = disabled is None and "disabled" not in cls
                    await browser.close()
                    return StockResult(in_stock=in_stock, name=name)

            for sel in _OOS_SELECTORS:
                if await page.query_selector(sel):
                    await browser.close()
                    return StockResult(in_stock=False, name=name)

            btn = await page.query_selector(_ADD_TO_CART)
            if btn is not None:
                disabled = await btn.get_attribute("disabled")
                cls = (await btn.get_attribute("class")) or ""
                in_stock = disabled is None and "disabled" not in cls
                await browser.close()
                return StockResult(in_stock=in_stock, name=name)

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

            await browser.close()
            return StockResult(
                in_stock=None,
                name=name,
                error="No se pudo detectar el estado del stock automáticamente",
            )

    except Exception as exc:
        logger.exception("Unexpected scraper error for %s", url)
        return StockResult(in_stock=None, name=None, error=f"Error inesperado: {str(exc)[:120]}")
