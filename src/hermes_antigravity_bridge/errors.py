"""Typed bridge errors and their HTTP mappings."""

from __future__ import annotations


class BridgeError(Exception):
    status_code = 500
    error_type = "bridge_error"


class ConfigurationError(BridgeError):
    status_code = 500
    error_type = "configuration_error"


class InvalidRequest(BridgeError):
    status_code = 400
    error_type = "invalid_request_error"


class PromptTooLarge(InvalidRequest):
    pass


class UnknownModel(InvalidRequest):
    pass


class BackendError(BridgeError):
    status_code = 502
    error_type = "upstream_error"


class BackendUnavailable(BackendError):
    status_code = 503


class BackendTimeout(BackendError):
    status_code = 504
    error_type = "timeout_error"


class BackendProtocolError(BackendError):
    pass


class ToolIsolationError(BackendError):
    error_type = "tool_isolation_error"


class InvalidToolCall(BackendProtocolError):
    error_type = "invalid_tool_call"
