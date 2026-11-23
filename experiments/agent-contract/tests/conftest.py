import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(EXPERIMENTS / "hy4-research-policy"))
