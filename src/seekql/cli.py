"""Command-line entry point for seekql."""

from seekql import __version__


def main() -> None:
    print(f"seekql {__version__}")


if __name__ == "__main__":
    main()
