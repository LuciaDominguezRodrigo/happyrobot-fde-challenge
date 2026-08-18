import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from otp import LoggingNotificationSender, MAX_VERIFY_ATTEMPTS, OtpService


def test_correct_code_verifies():
    sender = LoggingNotificationSender()
    service = OtpService(sender)
    service.send_otp("call-1", "carrier@example.com")
    destination, message = sender.sent[0]
    assert destination == "carrier@example.com"
    code = "".join(ch for ch in message if ch.isdigit())[:6]

    assert service.verify_otp("call-1", code) is True
    assert service.is_verified("call-1") is True


def test_wrong_code_does_not_verify():
    sender = LoggingNotificationSender()
    service = OtpService(sender)
    service.send_otp("call-1", "carrier@example.com")

    assert service.verify_otp("call-1", "000000") is False
    assert service.is_verified("call-1") is False


def test_verify_locks_out_after_max_attempts():
    sender = LoggingNotificationSender()
    service = OtpService(sender)
    service.send_otp("call-1", "carrier@example.com")

    for _ in range(MAX_VERIFY_ATTEMPTS):
        assert service.verify_otp("call-1", "000000") is False

    # even the real code is now rejected -- attempts are exhausted
    _, message = sender.sent[0]
    code = "".join(ch for ch in message if ch.isdigit())[:6]
    assert service.verify_otp("call-1", code) is False


def test_unknown_call_id_does_not_verify():
    sender = LoggingNotificationSender()
    service = OtpService(sender)
    assert service.verify_otp("no-such-call", "123456") is False
