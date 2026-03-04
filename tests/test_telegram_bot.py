import time

from app.channels.telegram.bot import _chunk_text, _TypingPulse


def test_chunk_text_splits_long_messages() -> None:
    text = "a" * 9001
    chunks = _chunk_text(text, chunk_size=3500)
    assert len(chunks) == 3
    assert "".join(chunks) == text
    assert max(len(chunk) for chunk in chunks) <= 3500


def test_typing_pulse_calls_sender_until_stopped() -> None:
    calls = 0

    def send_typing() -> None:
        nonlocal calls
        calls += 1

    with _TypingPulse(send_typing, interval_sec=0.01):
        time.sleep(0.04)
    assert calls >= 1
