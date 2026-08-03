from console.bootstrap import ensure_src_path

ensure_src_path()

from console.control.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
