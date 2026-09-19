import json

from bandwidtharr.state import SharedState


def _update_kwargs(link_event=None):
    return dict(
        total=100.0, qbit_speed=0.0, qbit_limit=100.0, sab_speed=0.0, sab_limit=100.0,
        qbit_ok=True, sab_ok=True, link_event=link_event,
    )


def test_link_events_not_persisted_without_a_file():
    state = SharedState()
    state.update(**_update_kwargs(link_event=(1.0, "primary", "backup", 800.0, 50.0)))
    assert state.snapshot()["link_events"] == [(1.0, "primary", "backup", 800.0, 50.0)]


def test_link_events_persist_and_reload(tmp_path):
    events_file = str(tmp_path / "events.json")
    state = SharedState(link_events_file=events_file)
    state.update(**_update_kwargs(link_event=(1.0, "primary", "backup", 800.0, 50.0)))
    state.update(**_update_kwargs(link_event=(2.0, "backup", "primary", 50.0, 800.0)))

    reloaded = SharedState(link_events_file=events_file)
    assert reloaded.snapshot()["link_events"] == [
        (1.0, "primary", "backup", 800.0, 50.0),
        (2.0, "backup", "primary", 50.0, 800.0),
    ]


def test_link_events_load_caps_to_maxlen(tmp_path):
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps([[float(i), "primary", "backup", 800.0, 50.0] for i in range(10)]))

    state = SharedState(link_events_len=3, link_events_file=str(events_file))
    events = state.snapshot()["link_events"]
    assert len(events) == 3
    assert events[-1][0] == 9.0  # keeps the most recent


def test_link_events_missing_file_starts_empty(tmp_path):
    events_file = str(tmp_path / "does_not_exist.json")
    state = SharedState(link_events_file=events_file)
    assert state.snapshot()["link_events"] == []


def test_link_events_corrupt_file_starts_empty(tmp_path):
    events_file = tmp_path / "events.json"
    events_file.write_text("not valid json")

    state = SharedState(link_events_file=str(events_file))
    assert state.snapshot()["link_events"] == []


def test_update_without_link_event_does_not_touch_persisted_file(tmp_path):
    events_file = tmp_path / "events.json"
    state = SharedState(link_events_file=str(events_file))
    state.update(**_update_kwargs(link_event=None))
    assert not events_file.exists()
