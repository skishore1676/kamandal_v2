from types import SimpleNamespace

import pytest

from kamandal_v2.sheets import GoogleSheetClient, is_transient_sheet_error, retry_transient_sheet_call


class FakeSheetError(RuntimeError):
    def __init__(self, status_code: int, message: str = "sheet error") -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status_code)


class FakeWorksheetNotFound(RuntimeError):
    pass


def test_retry_transient_sheet_call_recovers_after_503() -> None:
    calls = []
    sleeps = []

    def flaky_call() -> str:
        calls.append("call")
        if len(calls) < 3:
            raise FakeSheetError(503, "APIError: [503]: service unavailable")
        return "ok"

    result = retry_transient_sheet_call(
        flaky_call,
        operation="read daily_plan",
        attempts=3,
        base_delay_seconds=1,
        max_delay_seconds=4,
        sleep=sleeps.append,
    )

    assert result == "ok"
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_retry_transient_sheet_call_does_not_retry_permanent_error() -> None:
    calls = []

    def permanent_failure() -> None:
        calls.append("call")
        raise FakeSheetError(400, "bad request")

    with pytest.raises(FakeSheetError):
        retry_transient_sheet_call(
            permanent_failure,
            operation="update daily_plan",
            attempts=3,
            sleep=lambda _delay: None,
        )

    assert calls == ["call"]
    assert is_transient_sheet_error(FakeSheetError(400)) is False


def test_worksheet_retries_transient_lookup_without_creating_duplicate() -> None:
    lookup_calls = []
    add_calls = []
    worksheet = object()

    class FakeSpreadsheet:
        def worksheet(self, _title):
            lookup_calls.append("lookup")
            if len(lookup_calls) == 1:
                raise FakeSheetError(503, "APIError: [503]: service unavailable")
            return worksheet

        def add_worksheet(self, **kwargs):
            add_calls.append(kwargs)
            return object()

    client = GoogleSheetClient.__new__(GoogleSheetClient)
    client._spreadsheet = FakeSpreadsheet()
    client._worksheet_not_found = FakeWorksheetNotFound
    client._retry_attempts = 3
    client._retry_base_delay_seconds = 0.0
    client._retry_max_delay_seconds = 0.0
    client._sleep = lambda _delay: None

    assert client._worksheet("daily_plan", rows=100, cols=26) is worksheet
    assert lookup_calls == ["lookup", "lookup"]
    assert add_calls == []


def test_worksheet_creates_tab_only_for_not_found() -> None:
    add_calls = []
    created = object()

    class FakeSpreadsheet:
        def worksheet(self, _title):
            raise FakeWorksheetNotFound("missing")

        def add_worksheet(self, **kwargs):
            add_calls.append(kwargs)
            return created

    client = GoogleSheetClient.__new__(GoogleSheetClient)
    client._spreadsheet = FakeSpreadsheet()
    client._worksheet_not_found = FakeWorksheetNotFound
    client._retry_attempts = 3
    client._retry_base_delay_seconds = 0.0
    client._retry_max_delay_seconds = 0.0
    client._sleep = lambda _delay: None

    assert client._worksheet("daily_plan", rows=100, cols=26) is created
    assert add_calls == [{"title": "daily_plan", "rows": 100, "cols": 26}]
