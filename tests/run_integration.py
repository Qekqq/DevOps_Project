"""Install test-only dependencies into ephemeral storage on the isolated CI stack."""

import os
import sys


def main():
    if os.getenv("RUN_INTEGRATION_TESTS") != "1":
        raise SystemExit(
            "Integration dependencies are only installed on the test stack"
        )
    destination = "/test-packages"
    # Keep the administrator engine in this one-shot process. Spawning pytest
    # would lose it and accidentally run fixtures with the limited API identity.
    sys.path.insert(0, destination)
    import pytest

    raise SystemExit(pytest.main(sys.argv[1:]))


if __name__ == "__main__":
    main()
