"""Platform detection for Briefcase template directories.

Each of the Briefcase template repos this tooling supports has an
unambiguous platform name in its directory name:

    briefcase-iOS-Xcode-template -> iOS
    briefcase-linux-appimage-template -> linux
    briefcase-linux-flatpak-template -> linux
    briefcase-macOS-app-template -> macOS
    briefcase-macOS-Xcode-template -> macOS
    briefcase-windows-app-template -> windows
    briefcase-windows-VisualStudio-template -> windows
"""

from __future__ import annotations

from pathlib import Path

# Ordered (substring, platform) pairs; checked in order against the
# lower-cased directory basename.
_PLATFORM_SUBSTRINGS = ["macOS", "iOS", "windows", "linux"]


def detect_platform(template_dir: Path) -> str:
    """Infer the platform from a template directory's basename."""
    name = Path(template_dir).name.lower()
    for platform in _PLATFORM_SUBSTRINGS:
        if platform.lower() in name.lower():
            return platform
    raise ValueError(
        f"Could not detect platform from template directory {str(template_dir)!r}; "
        "expected the directory name to contain one of: macos, ios, windows, linux"
    )


def briefcase_toml_path(template_dir: Path) -> Path:
    """Path to the (unrendered) `briefcase.toml` jinja template inside a
    template directory. The `{{ cookiecutter.format }}` directory name is
    literal/unrendered in all four templates."""
    return Path(template_dir) / "{{ cookiecutter.format }}" / "briefcase.toml"
