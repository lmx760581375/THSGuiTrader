class GatewayError(RuntimeError):
    """Expected error while reading the trading client."""


class AccessibilityUnavailable(GatewayError):
    """The macOS Accessibility API cannot be used or the app is unavailable."""


class DataValidationError(GatewayError):
    """The client exposed incomplete or inconsistent data."""
