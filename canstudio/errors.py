"""异常类型。

诊断工具的报错分三类，混在一起会让上层无法处理：
  - 传输没回音（超时）：总线、接线、ECU 不在线
  - ECU 明确拒绝（否定响应）：请求本身有问题，带 NRC
  - 响应格式不对：对端不符合标准，通常是版本不匹配
"""


class CanStudioError(Exception):
    """所有诊断相关异常的基类。"""


class TransportTimeout(CanStudioError):
    """在 P2/P2* 窗口内没有收到响应。"""

    def __init__(self, message="no response within the P2 window", waited_ms=0):
        super().__init__(f"{message} (waited {waited_ms} ms)")
        self.waited_ms = waited_ms


class NegativeResponse(CanStudioError):
    """ECU 回了 7F <sid> <nrc>。"""

    def __init__(self, service, nrc, raw=b""):
        self.service = service
        self.nrc = nrc
        self.raw = bytes(raw)
        super().__init__(
            f"service 0x{service:02X} rejected with NRC 0x{nrc:02X}"
            f" ({NRC_NAMES.get(nrc, 'unknown')})"
        )


class BadResponse(CanStudioError):
    """响应能收到，但内容不符合预期：SID 不对、长度不对、回显不匹配。"""


NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLength",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceivedResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}
