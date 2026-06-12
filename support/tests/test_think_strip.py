"""Tests for stripping model <think> reasoning out of customer replies.

MiniMax-M2.7 (and other reasoning models) emit their chain-of-thought inline as
`<think>...</think>` in the response content. Customers must never see it.
"""

from support.ai.backends import strip_think, ThinkStreamFilter


# --- non-streaming (complete) ---

def test_strip_think_removes_block():
    assert strip_think("<think>let me reason</think>Hello!") == "Hello!"


def test_strip_think_multiline_and_trims():
    assert strip_think("<think>\nstep 1\nstep 2\n</think>\n\nThe answer is 42.") == "The answer is 42."


def test_strip_think_no_block_passes_through():
    assert strip_think("Just a normal reply.") == "Just a normal reply."


def test_strip_think_multiple_blocks():
    assert strip_think("<think>a</think>Hi <think>b</think>there") == "Hi there"


def test_strip_think_unclosed_block_dropped():
    # malformed / truncated reasoning — drop everything from the open tag
    assert strip_think("Answer ready<think>still reasoning...") == "Answer ready"


# --- streaming ---

def _drain(chunks):
    f = ThinkStreamFilter()
    out = "".join(f.feed(c) for c in chunks)
    out += f.flush()
    return out, f


def test_stream_suppresses_think():
    out, f = _drain(["<think>reason", "ing here</think>", "Hello ", "world"])
    assert out == "Hello world"
    assert f.text == "Hello world"


def test_stream_tag_split_across_chunks():
    out, _ = _drain(["<th", "ink>secret rea", "soning</thi", "nk>Answer:", " 42"])
    assert out == "Answer: 42"


def test_stream_no_think_streams_normally():
    out, _ = _drain(["Hel", "lo ", "there"])
    assert out == "Hello there"


def test_stream_never_emits_partial_tag():
    f = ThinkStreamFilter()
    emitted = [f.feed(c) for c in ["Hi <thi", "nk>hidden</think> bye"]]
    emitted.append(f.flush())
    joined = "".join(emitted)
    assert "<think" not in joined
    assert "hidden" not in joined
    assert joined == "Hi  bye"


def test_stream_lone_lt_is_not_swallowed():
    # a real '<' that is not the start of a think tag must still be delivered
    out, _ = _drain(["price < ", "100 ok"])
    assert out == "price < 100 ok"
