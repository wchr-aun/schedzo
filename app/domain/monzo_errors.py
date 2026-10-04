"""Provider failures shared by services and HTTP error adapters."""


class MonzoError(Exception):
    """Sanitized provider failure safe to translate at an API boundary."""


class MonzoUnavailableError(MonzoError):
    pass


class MonzoInvalidResponseError(MonzoError):
    pass


class MonzoRequestError(MonzoError):
    def __init__(self, status_code: int, *, approval_required: bool = False):
        super().__init__("Monzo request failed")
        self.status_code = status_code
        self.approval_required = approval_required
