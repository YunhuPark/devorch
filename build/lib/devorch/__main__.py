"""``python -m devorch ...`` — same CLI as the ``devorch`` command, without depending on the installed launcher."""

from devorch.cli.main import app

if __name__ == "__main__":
    app()
