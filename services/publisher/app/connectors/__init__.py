from app.connectors.base import ConnectorError, StoreConnector
from app.connectors.custom import CustomStoreConnector
from app.connectors.shopify import ShopifyConnector
from app.connectors.woocommerce import WooCommerceConnector

# Keys are platform names. custom = stores with the Listing API (Smart Click).
_REGISTRY: dict[str, type[StoreConnector]] = {
    "custom": CustomStoreConnector,
    "woocommerce": WooCommerceConnector,
    "shopify": ShopifyConnector,
}


class ConnectorNotBuilt(Exception):
    pass


def get_connector(platform: str, store_url: str, credentials: dict) -> StoreConnector:
    cls = _REGISTRY.get(platform)
    if cls is None:
        raise ConnectorNotBuilt(f"The {platform} connector isn't built yet.")
    return cls(store_url, credentials)


__all__ = ["ConnectorError", "ConnectorNotBuilt", "get_connector"]
