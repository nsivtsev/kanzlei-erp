"""Public error codes never include private exception text."""


class ConnectorError(Exception):
	def __init__(self, code, status=400):
		super().__init__(code)
		self.code = code
		self.status = status


def fail(code="INVALID_REQUEST", status=400):
	raise ConnectorError(code, status)


def text(value, maximum=200):
	if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
		fail()
	return value
