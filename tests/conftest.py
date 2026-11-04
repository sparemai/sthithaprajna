"""The suite pins the v3.0 bounded risk mode for the tests written against the spec; tests of the operator's
unbounded mode (D-031) build their config explicitly. Set before any config is loaded (load_operator caches)."""
import os

os.environ["STHITAPRAJNA_RISK_MODE"] = "bounded"
