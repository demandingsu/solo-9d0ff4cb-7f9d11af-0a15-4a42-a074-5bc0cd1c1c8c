#!/usr/bin/env python3
"""对比「按批次取数」在无关数据增多前后的读入行数。

用法: .venv/bin/python scripts/measure_read_scope.py
使用独立临时 SQLite,不受现有数据影响;直接调用服务层取数并打印统计。
"""
from __future__ import annotations

import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="gauge-measure-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/measure.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app import services  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Claim, Item, Workstation  # noqa: E402


def op_item(code, nominal, tol):
    return {
        "op": "upsert_item",
        "item_code": code,
        "name": code,
        "nominal_um": nominal,
        "upper_um": nominal + tol,
        "lower_um": nominal - tol,
    }


def op_ws(code, gen="new"):
    return {"op": "upsert_workstation", "workstation_code": code, "name": code, "terminal_generation": gen}


def op_claim(ws, item):
    return {"op": "claim_item", "workstation_code": ws, "item_code": item}


def apply(client, ops):
    bid = client.post("/batches", json={"title": "", "ops": ops}).json()["id"]
    assert client.post(f"/batches/{bid}/verify").status_code == 200
    assert client.post(f"/batches/{bid}/publish").status_code == 200


def db_rows():
    with SessionLocal() as s:
        return (
            s.scalar(select(func.count()).select_from(Item)),
            s.scalar(select(func.count()).select_from(Workstation)),
            s.scalar(select(func.count()).select_from(Claim)),
        )


def measure(ops):
    with SessionLocal() as s:
        state = services.load_state_for_ops(s, ops)
        return state.stats


def main():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)

    # 基础数据:2 个项目、2 个工位、3 条领用
    apply(
        client,
        [
            op_item("ITEM-A", 10000, 5),
            op_item("ITEM-B", 20000, 8),
            op_ws("WS-1", "new"),
            op_ws("WS-2", "old"),
            op_claim("WS-1", "ITEM-A"),
            op_claim("WS-1", "ITEM-B"),
            op_claim("WS-2", "ITEM-A"),
        ],
    )
    ops = [op_item("ITEM-A", 10000, 6)]  # 只改一项公差

    s1 = measure(ops)
    rows1 = db_rows()
    print("== 无关数据灌入前 ==")
    print(f"数据库行数: 项目={rows1[0]} 工位={rows1[1]} 领用={rows1[2]}")
    print(
        f"本次读入: 项目={s1.item_rows} 工位={s1.workstation_rows} 领用={s1.claim_rows}"
        f" 共={s1.total_rows} 行,{s1.queries} 条查询"
    )

    # 灌入 200 个无关项目 / 工位 / 领用
    unrelated = []
    for i in range(200):
        unrelated += [
            op_item(f"OTHER-I{i:03d}", 1000, 2),
            op_ws(f"OTHER-W{i:03d}"),
            op_claim(f"OTHER-W{i:03d}", f"OTHER-I{i:03d}"),
        ]
    apply(client, unrelated)

    s2 = measure(ops)
    rows2 = db_rows()
    print("\n== 无关数据灌入后(新增 200 项目/200 工位/200 领用)==")
    print(f"数据库行数: 项目={rows2[0]} 工位={rows2[1]} 领用={rows2[2]}")
    print(
        f"本次读入: 项目={s2.item_rows} 工位={s2.workstation_rows} 领用={s2.claim_rows}"
        f" 共={s2.total_rows} 行,{s2.queries} 条查询"
    )
    print(
        f"\n全库读入(旧实现)会从 {sum(rows1)} 行增至 {sum(rows2)} 行;"
        f"按批次取数保持 {s1.total_rows} → {s2.total_rows} 行。"
    )
    assert (s1.item_rows, s1.workstation_rows, s1.claim_rows) == (
        s2.item_rows,
        s2.workstation_rows,
        s2.claim_rows,
    )
    assert s1.queries == s2.queries


if __name__ == "__main__":
    main()
