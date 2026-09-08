from pathlib import Path, PureWindowsPath

import pytest

from grayson.util import ensure_within


@pytest.mark.parametrize("extended_root", [False, True])
@pytest.mark.parametrize(
    "root, extended",
    [
        ("C:\\checks", "\\\\?\\C:\\checks"),
        ("\\\\server\\share\\checks", "\\\\?\\UNC\\server\\share\\checks"),
    ],
)
def test_windows_mixed_resolved_prefixes_do_not_reject_descendants(
    monkeypatch, tmp_path, root, extended, extended_root
):
    # Model realpath's two legal outputs when another worker creates a parent
    # during its OS probes. PureWindowsPath makes the regression portable.
    parent = tmp_path / "checks"
    target = parent / "runs" / "result.json"
    resolved_root = PureWindowsPath(extended if extended_root else root)
    resolved_target = PureWindowsPath(root if extended_root else extended) / "runs" / "result.json"
    monkeypatch.setattr(
        Path, "resolve", lambda path: resolved_root if path == parent else resolved_target
    )
    assert ensure_within(parent, target) is resolved_target


@pytest.mark.parametrize(
    "outside",
    [
        "\\\\?\\C:\\checks-other\\result.json",
        "\\\\?\\D:\\checks\\result.json",
        "\\\\?\\UNC\\server\\other-share\\checks\\result.json",
        "\\\\?\\Volume{different}\\checks\\result.json",
        "\\\\.\\C:\\checks\\result.json",
    ],
)
def test_windows_prefix_normalization_does_not_allow_other_roots(monkeypatch, tmp_path, outside):
    parent = tmp_path / "checks"
    target = parent / "result.json"
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path: PureWindowsPath("C:\\checks") if path == parent else PureWindowsPath(outside),
    )
    with pytest.raises(ValueError, match="escapes"):
        ensure_within(parent, target)


def test_containment_still_resolves_traversal_and_symlinks(tmp_path):
    root = tmp_path / "checks"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        ensure_within(root, root / ".." / "outside" / "result.json")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="escapes"):
        ensure_within(root, link / "result.json")
