from __future__ import annotations

import pytest

from forgectl import __version__
from forgectl.cli import main


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"forgectl {__version__}"


def test_no_arguments_returns_usage_error() -> None:
    assert main([]) == 2
