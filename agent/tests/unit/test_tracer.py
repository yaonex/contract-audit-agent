import threading

from audit_agent.observability.tracer import Tracer, read_events


def make_tracer(tmp_path) -> Tracer:
    return Tracer(run_id="run1", base_dir=tmp_path)


def test_span_writes_one_readable_line(tmp_path):
    t = make_tracer(tmp_path)
    with t.span("llm", "chat") as sp:
        sp.set(model="m1", prompt_tokens=10, completion_tokens=5, cache_hit=False, note="x")
    events = read_events(t.path)
    assert len(events) == 1
    e = events[0]
    assert (e.kind, e.name, e.status) == ("llm", "chat", "ok")
    assert (e.model, e.prompt_tokens, e.completion_tokens, e.cache_hit) == ("m1", 10, 5, False)
    assert e.attrs == {"note": "x"}  # non-typed fields land in attrs
    assert e.duration_ms >= 0


def test_error_is_recorded_and_reraised(tmp_path):
    t = make_tracer(tmp_path)
    try:
        with t.span("tool", "boom"):
            raise ValueError("bad input")
    except ValueError:
        pass
    else:
        raise AssertionError("exception must propagate")
    e = read_events(t.path)[0]
    assert e.status == "error"
    assert e.error is not None
    assert "ValueError: bad input" in e.error


def test_nested_spans_link_parent(tmp_path):
    t = make_tracer(tmp_path)
    with t.span("node", "outer"):
        with t.span("llm", "inner"):
            pass
    by_name = {e.name: e for e in read_events(t.path)}
    assert by_name["inner"].parent_id == by_name["outer"].span_id
    assert by_name["outer"].parent_id is None


def test_sibling_spans_are_not_parent_child(tmp_path):
    t = make_tracer(tmp_path)
    with t.span("tool", "a"):
        pass
    with t.span("tool", "b"):
        pass
    assert all(e.parent_id is None for e in read_events(t.path))


def test_blob_roundtrip_and_name_sanitized(tmp_path):
    t = make_tracer(tmp_path)
    rel = t.write_blob("../../evil name", "full forge log")
    assert ".." not in rel
    assert (tmp_path / rel).read_text(encoding="utf-8") == "full forge log"
    assert rel.startswith("run1/blobs/")


def test_blob_always_stays_inside_blobs_dir(tmp_path):
    t = make_tracer(tmp_path)
    for name in ["../../x", "/etc/passwd", "a/b\\c", "....", "", "名字"]:
        rel = t.write_blob(name, "data")
        resolved = (tmp_path / rel).resolve()
        assert resolved.is_relative_to((tmp_path / "run1" / "blobs").resolve()), name


def test_concurrent_writes_stay_line_intact(tmp_path):
    t = make_tracer(tmp_path)

    def work():
        for _ in range(50):
            with t.span("tool", "w"):
                pass

    threads = [threading.Thread(target=work) for _ in range(8)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert len(read_events(t.path)) == 400  # every line parses, none lost


def test_write_failure_does_not_break_caller(tmp_path):
    t = make_tracer(tmp_path)
    t.path = tmp_path  # a directory: open() for append will fail
    with t.span("tool", "x"):
        result = 1 + 1
    assert result == 2  # business code unaffected