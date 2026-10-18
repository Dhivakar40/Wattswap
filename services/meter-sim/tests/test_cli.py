import pytest

from meter_sim.cli import build_parser, main


def test_help_mentions_simulator(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "SIMULATOR" in capsys.readouterr().out


def test_parser_prog() -> None:
    assert build_parser().prog == "meter-sim"
