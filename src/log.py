import os, logging
from .config import Cfg

LOG_DIR = Cfg.DATA_DIR
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | memesniper | %(message)s"
)

logger = logging.getLogger("memesniper")
