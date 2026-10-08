"""Safe domain errors shared by HTTP routes, workers and model transport."""


class DocumentError(ValueError):
    pass


class ProviderError(DocumentError):
    def __init__(self, message, *, code='api_error', http_status=502,
                 stage='response', retryable=False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.stage = stage
        self.retryable = retryable

    def public_info(self):
        return {'code': self.code, 'stage': self.stage, 'retryable': self.retryable}

    def worker_payload(self):
        return {'message': str(self), 'http_status': self.http_status, **self.public_info()}
