"""Tests for version comparison used by the in-app updater."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slimbms import updater  # noqa: E402


def test_parse_version():
    assert updater.parse_version("v0.4.0") == (0, 4, 0)
    assert updater.parse_version("0.4.0") == (0, 4, 0)
    assert updater.parse_version("v1.2") == (1, 2)
    assert updater.parse_version("v0.4.0-beta") == (0, 4, 0)


def test_is_newer():
    assert updater.is_newer("v0.5.0", "0.4.0")
    assert updater.is_newer("v0.4.1", "0.4.0")
    assert updater.is_newer("v1.0.0", "0.9.9")
    assert not updater.is_newer("v0.4.0", "0.4.0")
    assert not updater.is_newer("v0.3.0", "0.4.0")
    assert not updater.is_newer("v0.4.0", "0.4.1")


def test_shorter_tag_not_newer_than_patch():
    # "v0.4" == (0,4) < (0,4,0) is False; treat equal-ish releases sanely.
    assert not updater.is_newer("v0.4", "0.4.0")


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
