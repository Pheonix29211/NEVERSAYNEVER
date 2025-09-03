from __future__ import annotations
import os, json
from typing import Any, Dict
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, JSON
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import func
from .config import DATA_DIR
from .log import logger

DB_PATH = os.path.join(DATA_DIR, "bot.db")
SNAP_PATH = os.path.join(DATA_DIR, "portfolio_snapshot.json")
os.makedirs(DATA_DIR, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    token = Column(String)
    ca = Column(String)
    lane = Column(String)   # SAFE/GIANT/INSIDER
    side = Column(String)   # BUY/SELL
    qty = Column(Float)
    px = Column(Float)
    notional_usd = Column(Float)
    fees_pct = Column(Float)
    slip_pct = Column(Float)
    reason = Column(String)
    ts = Column(DateTime, server_default=func.now())

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    token = Column(String, index=True)
    ca = Column(String, index=True)
    mode = Column(String)  # NORMAL/INSIDER/GIANT
    qty = Column(Float, default=0.0)
    avg = Column(Float, default=0.0)
    last_px = Column(Float, default=0.0)
    meta = Column(JSON, default={})

class Param(Base):
    __tablename__ = "params"
    key = Column(String, primary_key=True)
    val = Column(String)

def init_db():
    Base.metadata.create_all(bind=engine)
    logger.info(f"DB initialized at {DB_PATH}")

def save_snapshot(data: Dict[str, Any]):
    with open(SNAP_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_snapshot() -> Dict[str, Any]:
    if not os.path.exists(SNAP_PATH):
        return {}
    with open(SNAP_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

init_db()
