"""Validate offliner-definition.json with Zimfarm's own schema.

    PYTHONPATH=<zimfarm>/backend/src python tools/check_zimfarm_schema.py [offliner-definition.json]

Uses zimfarm_backend's OfflinerSpecSchema, the model Zimfarm checks an
uploaded definition against. Importing zimfarm_backend reads a few settings
from the environment at import time; dummy values are enough, nothing
connects anywhere. CI pins the Zimfarm commit (see .github/workflows/ci.yml).
"""
from __future__ import annotations

import json
import os
import sys

for key in ("POSTGRES_URI", "JWT_SECRET"):
    os.environ.setdefault(key, "unused")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "offliner-definition.json"
    from zimfarm_backend.common.schemas.offliners.models import OfflinerSpecSchema
    with open(path, encoding="utf-8") as f:
        definition = json.load(f)
    spec = OfflinerSpecSchema.model_validate(definition)
    print(f"ok: {path} is a valid Zimfarm offliner definition "
          f"({len(spec.flags)} flags)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
