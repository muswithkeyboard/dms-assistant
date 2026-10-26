from __future__ import annotations


def test_cli_help():
    from dms_core.cli import main

    try:
        main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    else:
        raise AssertionError("--help должен завершаться SystemExit(0)")
