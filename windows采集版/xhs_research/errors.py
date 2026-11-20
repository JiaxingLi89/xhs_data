class CollectorError(Exception):
    """Errors contain only safe diagnostic codes, never responses or credentials."""


class StopRun(CollectorError):
    pass


class SetupError(CollectorError):
    pass


class AuthExpired(CollectorError):
    pass


class NetworkFailure(CollectorError):
    pass


class BrowserDisconnected(NetworkFailure):
    pass


class ProtocolError(CollectorError):
    pass


class Unavailable(CollectorError):
    pass


class StaleLink(CollectorError):
    pass


class RiskBlocked(CollectorError):
    def __init__(self, reason="access_restricted", cooldown=0):
        super().__init__(reason)
        self.cooldown = max(0, cooldown)

