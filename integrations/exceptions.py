class ProviderError(Exception):
    """Base error raised by an external provider client."""


class ProviderAccessError(ProviderError):
    """Authentication or authorization failed and must not be retried."""


class ProviderResponseError(ProviderError):
    """The provider returned a malformed or non-retryable response."""


class ProviderTransientError(ProviderError):
    def __init__(self, message, *, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class ProviderSuspendedError(ProviderError):
    """The provider requested a delay longer than this job may safely wait."""
