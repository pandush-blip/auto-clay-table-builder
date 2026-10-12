import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="claybuilder-tests-")
os.environ["CLAYBUILDER_DB"] = str(Path(_TMP) / "test.db")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_EMAIL"] = "admin@test.local"
os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["CLAYBUILDER_INSECURE_COOKIES"] = "1"
os.environ.pop("OPENAI_API_KEY", None)
