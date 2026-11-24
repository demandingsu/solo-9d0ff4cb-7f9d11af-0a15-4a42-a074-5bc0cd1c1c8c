"""随机差分校验:按批次取数 vs 全库取数,核对/发布判定必须完全一致。

用法: .venv/bin/python scripts/diffcheck_scoped_loading.py
使用独立临时 SQLite;随机造数,直接对比两种取数下的判定结果。
"""
import os
import random
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="gauge-diff-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/diff.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app import services
from app.db import Base, SessionLocal, engine
from app.models import Claim, Item, Workstation


def load_state_full(session):
    """旧实现:整库读入,作为对比基准。"""
    items, item_by_id = {}, {}
    for it in session.scalars(select(Item)).all():
        items[it.code] = services.ItemState(
            it.code, it.name, it.nominal_um, it.upper_um, it.lower_um, it.version, it.claims_version
        )
        item_by_id[it.id] = it.code
    wss, ws_by_id = {}, {}
    for ws in session.scalars(select(Workstation)).all():
        wss[ws.code] = services.WorkstationState(
            ws.code, ws.name, ws.terminal_generation, ws.version, ws.claims_version
        )
        ws_by_id[ws.id] = ws.code
    claims = set()
    for cl in session.scalars(select(Claim)).all():
        claims.add((ws_by_id[cl.workstation_id], item_by_id[cl.item_id]))
    return services.State(items, wss, claims)


def build_random(world):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    rnd = random.Random(world)
    n_items = rnd.randint(0, 8)
    n_ws = rnd.randint(0, 6)
    with SessionLocal() as s:
        items = []
        for i in range(n_items):
            # 对称公差,保证初始生效状态合法
            nom = rnd.randint(1000, 5000)
            tol = rnd.randint(0, 20)
            it = Item(code=f"I{i}", name=f"项目{i}", nominal_um=nom, upper_um=nom + tol, lower_um=nom - tol)
            s.add(it)
            items.append(it)
        wss = []
        for j in range(n_ws):
            ws = Workstation(
                code=f"W{j}", name=f"工位{j}", terminal_generation=rnd.choice(["new", "old"])
            )
            s.add(ws)
            wss.append(ws)
        s.flush()
        for it in items:
            for ws in wss:
                if rnd.random() < 0.35:
                    s.add(Claim(workstation_id=ws.id, item_id=it.id))
        s.commit()
    return rnd, [i.code for i in items], [w.code for w in wss]


def random_ops(rnd, item_codes, ws_codes):
    ops = []
    for _ in range(rnd.randint(0, 7)):
        kind = rnd.choice(["upsert_item", "upsert_workstation", "claim_item", "unclaim_item"])
        if kind == "upsert_item":
            code = rnd.choice(item_codes) if item_codes and rnd.random() < 0.8 else "NEW-I"
            nom = rnd.randint(1000, 5000)
            up = nom + rnd.randint(0, 20)
            lo = nom - rnd.randint(0, 20)
            ops.append(
                {"op": kind, "item_code": code, "name": "n", "nominal_um": nom,
                 "upper_um": up, "lower_um": lo}
            )
        elif kind == "upsert_workstation":
            code = rnd.choice(ws_codes) if ws_codes and rnd.random() < 0.8 else "NEW-W"
            ops.append(
                {"op": kind, "workstation_code": code, "name": "w",
                 "terminal_generation": rnd.choice(["new", "old"])}
            )
        else:
            if not item_codes or not ws_codes:
                continue
            ops.append(
                {"op": kind, "workstation_code": rnd.choice(ws_codes),
                 "item_code": rnd.choice(item_codes)}
            )
    return ops


def main():
    checked = 0
    worlds = int(os.environ.get("WORLDS", "400"))
    for world in range(worlds):
        rnd, item_codes, ws_codes = build_random(world)
        ops = random_ops(rnd, item_codes, ws_codes)
        with SessionLocal() as s:
            full = load_state_full(s)
            scoped = services.load_state_for_ops(s, ops)
        try:
            sim_full = services.simulate(full, ops)
            sim_scoped = services.simulate(scoped, ops)
        except services.OpError as exc:
            # 两种取数都应在同一操作上报错
            try:
                services.simulate(scoped, ops)
            except services.OpError:
                continue
            raise AssertionError(f"world={world}: full 报错但 scoped 未报错: {exc}")

        vf = services.find_violations(sim_full)
        vs = services.find_violations(sim_scoped)
        assert vf == vs, (world, ops, vf, vs)

        pf = services.build_preview(sim_full, ops)
        ps = services.build_preview(sim_scoped, ops)
        assert pf == ps, (world, ops, pf, ps)

        df = services.diff_states(full, sim_full)
        ds = services.diff_states(scoped, sim_scoped)
        assert df == ds, (world, ops, df, ds)

        ff = services.build_fingerprint(ops, full)
        fs = services.build_fingerprint(ops, scoped)
        assert ff == fs, (world, ops, ff, fs)
        assert services.fingerprint_changes(ff, full) == services.fingerprint_changes(fs, scoped)
        checked += 1
    print(f"OK: {checked} 个合法随机场景,核对违规/预览/净变更/指纹完全一致")


if __name__ == "__main__":
    main()
