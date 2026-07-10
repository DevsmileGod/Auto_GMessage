"""Custom exceptions for mail providers."""


class ProviderError(Exception):
    """Base exception for provider operations."""

    pass


class AuthenticationError(ProviderError):
    """Failed to authenticate with provider."""

    pass


class RateLimitError(ProviderError):
    """Provider rate limit exceeded."""

    pass


class SendError(ProviderError):
    """Failed to send email."""

    pass


class ConfigurationError(ProviderError):
    """Provider configuration is invalid."""

    pass
