"""核心业务逻辑:状态快照、批次模拟、核对、指纹失效判定、发布落库。

并发模型:核对(verify)与发布(publish)在同一进程内由 STATE_LOCK 串行化,
保证「核对指纹 -> 发布落库」之间不被其他核对/发布交错(单 uvicorn worker 部署)。

取数模型:核对/发布只读取与批次**相关**的工位、项目和领用(见 load_state_for_ops),
不再把全库载入内存。相关范围:

1. 批次点名的工位与项目(模拟操作必须);
2. 领用了批次所涉项目的工位(即使批次没点它的名),用于按批末领用关系检查旧终端公差;
3. 上述相关工位领用的全部项目(补齐预览),但这些项目不再反向扩展工位范围,
   也不进入核对指纹;共享项目只读取一次。
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Batch, Claim, Item, Workstation, utcnow

STATE_LOCK = threading.RLock()


class OpError(Exception):
    """批次操作无法应用(如引用了不存在的工位/项目)。"""


# ---------------------------------------------------------------- 状态快照


@dataclass
class ItemState:
    code: str
    name: str
    nominal_um: int
    upper_um: int
    lower_um: int
    version: int = 1
    claims_version: int = 0

    @property
    def symmetric(self) -> bool:
        return self.upper_um - self.nominal_um == self.nominal_um - self.lower_um


@dataclass
class WorkstationState:
    code: str
    name: str
    terminal_generation: str
    version: int = 1
    claims_version: int = 0


@dataclass
class LoadStats:
    """一次按批次取数实际读入的行数,用于确认取数范围不随无关数据增长。"""

    queries: int = 0
    item_rows: int = 0
    workstation_rows: int = 0
    claim_rows: int = 0

    @property
    def total_rows(self) -> int:
        return self.item_rows + self.workstation_rows + self.claim_rows


@dataclass
class State:
    items: dict[str, ItemState]
    workstations: dict[str, WorkstationState]
    claims: set[tuple[str, str]]  # (workstation_code, item_code)
    # 本次取数实际读入的行数(诊断用;不参与状态比较)
    stats: LoadStats = field(default_factory=LoadStats, compare=False, repr=False)


def load_state_for_ops(session: Session, ops: list[dict]) -> State:
    """只读取批次相关的工位、项目和领用,不把全库载入内存。

    相关范围分三步确定,后一步不会反向扩大前一步:

    1. 批次点名的工位与项目(模拟操作所需);
    2. 领用了所涉项目的工位——即使批次没点它的名,也要按批末领用关系
       检查旧终端公差;
    3. 上述相关工位领用的**全部项目**,用于列齐预览。这些只为补齐预览
       而读入的项目不会再去查找领用它们的其他工位(不扩大工位范围),
       也不进入核对指纹(不改变失效范围)。

    工位、领用、项目各一条批量查询,每个项目/工位/领用最多读入一次。
    """
    item_codes, ws_codes = involved_entities(ops)
    stats = LoadStats()
    workstations: dict[str, WorkstationState] = {}
    ws_code_by_id: dict[int, str] = {}

    # 1+2) 批次点名的工位 ∪ 领用了所涉项目的工位(后者批次可能没点名);
    #      用子查询在数据库侧去重,共用工位只读入一次。
    ws_conditions = []
    if ws_codes:
        ws_conditions.append(Workstation.code.in_(ws_codes))
    if item_codes:
        claiming_ws_ids = (
            select(Claim.workstation_id)
            .join(Item, Item.id == Claim.item_id)
            .where(Item.code.in_(item_codes))
        )
        ws_conditions.append(Workstation.id.in_(claiming_ws_ids))
    if ws_conditions:
        stats.queries += 1
        for ws in session.scalars(
            select(Workstation).where(or_(*ws_conditions)).order_by(Workstation.code)
        ).all():
            workstations[ws.code] = WorkstationState(
                ws.code, ws.name, ws.terminal_generation, ws.version, ws.claims_version
            )
            ws_code_by_id[ws.id] = ws.code
            stats.workstation_rows += 1

    # 3) 相关工位的全部领用关系(预览需要)
    ws_ids = list(ws_code_by_id)
    claim_rows: list[Claim] = []
    claimed_item_ids: set[int] = set()
    if ws_ids:
        stats.queries += 1
        claim_rows = list(session.scalars(select(Claim).where(Claim.workstation_id.in_(ws_ids))).all())
        stats.claim_rows = len(claim_rows)
        claimed_item_ids = {cl.item_id for cl in claim_rows}

    # 4) 相关项目:所涉项目 ∪ 相关工位领用的项目;共用项目只读入一次
    items: dict[str, ItemState] = {}
    item_code_by_id: dict[int, str] = {}
    if item_codes or claimed_item_ids:
        conditions = []
        if item_codes:
            conditions.append(Item.code.in_(item_codes))
        if claimed_item_ids:
            conditions.append(Item.id.in_(claimed_item_ids))
        stats.queries += 1
        for it in session.scalars(select(Item).where(or_(*conditions))).all():
            items[it.code] = ItemState(
                it.code, it.name, it.nominal_um, it.upper_um, it.lower_um, it.version, it.claims_version
            )
            item_code_by_id[it.id] = it.code
            stats.item_rows += 1

    claims: set[tuple[str, str]] = set()
    for cl in claim_rows:
        claims.add((ws_code_by_id[cl.workstation_id], item_code_by_id[cl.item_id]))
    return State(items, workstations, claims, stats)


# ---------------------------------------------------------------- 批次模拟


def apply_op(state: State, op: dict) -> None:
    kind = op.get("op")
    if kind == "upsert_item":
        code = op["item_code"]
        existing = state.items.get(code)
        state.items[code] = ItemState(
            code=code,
            name=op["name"],
            nominal_um=op["nominal_um"],
            upper_um=op["upper_um"],
            lower_um=op["lower_um"],
            version=existing.version if existing else 1,
            claims_version=existing.claims_version if existing else 0,
        )
    elif kind == "upsert_workstation":
        code = op["workstation_code"]
        existing = state.workstations.get(code)
        state.workstations[code] = WorkstationState(
            code=code,
            name=op["name"],
            terminal_generation=op["terminal_generation"],
            version=existing.version if existing else 1,
            claims_version=existing.claims_version if existing else 0,
        )
    elif kind == "claim_item":
        ws_code, item_code = op["workstation_code"], op["item_code"]
        if ws_code not in state.workstations:
            raise OpError(f"工位不存在: {ws_code}(同批中需先 upsert_workstation)")
        if item_code not in state.items:
            raise OpError(f"检验项目不存在: {item_code}(同批中需先 upsert_item)")
        state.claims.add((ws_code, item_code))
    elif kind == "unclaim_item":
        ws_code, item_code = op["workstation_code"], op["item_code"]
        if ws_code not in state.workstations:
            raise OpError(f"工位不存在: {ws_code}")
        if item_code not in state.items:
            raise OpError(f"检验项目不存在: {item_code}")
        state.claims.discard((ws_code, item_code))
    else:
        raise OpError(f"未知操作类型: {kind}")


def simulate(state: State, ops: list[dict]) -> State:
    """在当前生效状态的副本上按顺序应用整批操作,得到改完后的状态。"""
    sim = copy.deepcopy(state)
    for op in ops:
        apply_op(sim, op)
    return sim


def diff_states(old: State, new: State) -> list[dict]:
    """比较模拟前后状态,得出需要落库的净变更(批内改回原样不产生变更)。"""
    changes: list[dict] = []
    for code, item in new.items.items():
        prev = old.items.get(code)
        if prev is None:
            changes.append({"kind": "create_item", "code": code})
        elif (prev.name, prev.nominal_um, prev.upper_um, prev.lower_um) != (
            item.name,
            item.nominal_um,
            item.upper_um,
            item.lower_um,
        ):
            changes.append({"kind": "update_item", "code": code})
    for code, ws in new.workstations.items():
        prev = old.workstations.get(code)
        if prev is None:
            changes.append({"kind": "create_workstation", "code": code})
        elif (prev.name, prev.terminal_generation) != (ws.name, ws.terminal_generation):
            changes.append({"kind": "update_workstation", "code": code})
    for ws_code, item_code in sorted(new.claims - old.claims):
        changes.append({"kind": "claim", "workstation_code": ws_code, "item_code": item_code})
    for ws_code, item_code in sorted(old.claims - new.claims):
        changes.append({"kind": "unclaim", "workstation_code": ws_code, "item_code": item_code})
    return changes


# ---------------------------------------------------------------- 核对


def find_violations(state: State) -> list[dict]:
    """找出所有「旧终端工位领用了非对称公差项目」的(工位, 项目)对。"""
    violations = []
    for ws_code, item_code in sorted(state.claims):
        ws = state.workstations[ws_code]
        item = state.items[item_code]
        if ws.terminal_generation == "old" and not item.symmetric:
            violations.append(
                {
                    "workstation_code": ws_code,
                    "item_code": item_code,
                    "nominal_um": item.nominal_um,
                    "upper_um": item.upper_um,
                    "lower_um": item.lower_um,
                    "reason": "旧终端仅接受对称公差(upper-nominal == nominal-lower)",
                }
            )
    return violations


def involved_entities(ops: list[dict]) -> tuple[set[str], set[str]]:
    """批次所涉的检验项目与工位。"""
    item_codes: set[str] = set()
    ws_codes: set[str] = set()
    for op in ops:
        kind = op["op"]
        if kind == "upsert_item":
            item_codes.add(op["item_code"])
        elif kind == "upsert_workstation":
            ws_codes.add(op["workstation_code"])
        elif kind in ("claim_item", "unclaim_item"):
            ws_codes.add(op["workstation_code"])
            item_codes.add(op["item_code"])
    return item_codes, ws_codes


def build_fingerprint(ops: list[dict], state: State) -> dict:
    """核对时记录所涉实体的版本快照;实体尚不存在记为 None。"""
    item_codes, ws_codes = involved_entities(ops)
    fp_items = {}
    for code in sorted(item_codes):
        it = state.items.get(code)
        fp_items[code] = (
            None if it is None else {"version": it.version, "claims_version": it.claims_version}
        )
    fp_ws = {}
    for code in sorted(ws_codes):
        ws = state.workstations.get(code)
        fp_ws[code] = (
            None if ws is None else {"version": ws.version, "claims_version": ws.claims_version}
        )
    return {"items": fp_items, "workstations": fp_ws}


def fingerprint_changes(fingerprint: dict, state: State) -> list[str]:
    """核对之后,所涉规范/工位配置/领用是否变过(版本号为单调计数器,改回原样也算变过)。"""
    changes: list[str] = []
    for code, snap in (fingerprint or {}).get("items", {}).items():
        cur = state.items.get(code)
        cur_snap = None if cur is None else {"version": cur.version, "claims_version": cur.claims_version}
        if cur_snap != snap:
            changes.append(f"检验项目 {code}:规范或领用已变更")
    for code, snap in (fingerprint or {}).get("workstations", {}).items():
        cur = state.workstations.get(code)
        cur_snap = None if cur is None else {"version": cur.version, "claims_version": cur.claims_version}
        if cur_snap != snap:
            changes.append(f"工位 {code}:配置或领用已变更")
    return changes


def build_preview(sim: State, ops: list[dict]) -> list[dict]:
    """核对预览:相关工位(批次涉及的 + 领用了所涉项目的)改完后将领用的规范。"""
    item_codes, ws_codes = involved_entities(ops)
    affected = set(ws_codes)
    for ws_code, item_code in sim.claims:
        if item_code in item_codes:
            affected.add(ws_code)
    preview = []
    for ws_code in sorted(affected):
        ws = sim.workstations.get(ws_code)
        if ws is None:
            continue
        items = []
        for w, i in sorted(sim.claims):
            if w == ws_code:
                it = sim.items[i]
                items.append(
                    {
                        "item_code": i,
                        "name": it.name,
                        "nominal_um": it.nominal_um,
                        "upper_um": it.upper_um,
                        "lower_um": it.lower_um,
                        "symmetric": it.symmetric,
                    }
                )
        preview.append(
            {
                "workstation_code": ws_code,
                "terminal_generation": ws.terminal_generation,
                "items": items,
            }
        )
    return preview


# ---------------------------------------------------------------- 发布落库


def apply_changes(session: Session, changes: list[dict], sim: State) -> list[dict]:
    """把净变更写入数据库并联动换版(版本号 +1)。返回带新版本号的变更清单。"""
    now = utcnow()
    enriched: list[dict] = []

    # 1) 规范与工位配置
    for ch in changes:
        kind = ch["kind"]
        if kind == "create_item":
            s = sim.items[ch["code"]]
            session.add(
                Item(
                    code=s.code, name=s.name, nominal_um=s.nominal_um,
                    upper_um=s.upper_um, lower_um=s.lower_um, version=1, claims_version=0,
                )
            )
            enriched.append({**ch, "version": 1})
        elif kind == "update_item":
            it = session.scalar(select(Item).where(Item.code == ch["code"]))
            s = sim.items[ch["code"]]
            it.name, it.nominal_um, it.upper_um, it.lower_um = (
                s.name, s.nominal_um, s.upper_um, s.lower_um,
            )
            it.version += 1
            it.updated_at = now
            enriched.append({**ch, "version": it.version})
        elif kind == "create_workstation":
            s = sim.workstations[ch["code"]]
            session.add(
                Workstation(
                    code=s.code, name=s.name, terminal_generation=s.terminal_generation,
                    version=1, claims_version=0,
                )
            )
            enriched.append({**ch, "version": 1})
        elif kind == "update_workstation":
            ws = session.scalar(select(Workstation).where(Workstation.code == ch["code"]))
            s = sim.workstations[ch["code"]]
            ws.name, ws.terminal_generation = s.name, s.terminal_generation
            ws.version += 1
            ws.updated_at = now
            enriched.append({**ch, "version": ws.version})

    session.flush()  # 新建实体拿到主键,供同批领用引用

    # 2) 领用增删(每实体每批只换版一次)
    touched_items: set[str] = set()
    touched_ws: set[str] = set()
    for ch in changes:
        kind = ch["kind"]
        if kind == "claim":
            ws = session.scalar(select(Workstation).where(Workstation.code == ch["workstation_code"]))
            it = session.scalar(select(Item).where(Item.code == ch["item_code"]))
            session.add(Claim(workstation_id=ws.id, item_id=it.id))
            touched_items.add(it.code)
            touched_ws.add(ws.code)
            enriched.append(ch)
        elif kind == "unclaim":
            ws = session.scalar(select(Workstation).where(Workstation.code == ch["workstation_code"]))
            it = session.scalar(select(Item).where(Item.code == ch["item_code"]))
            row = session.scalar(
                select(Claim).where(Claim.workstation_id == ws.id, Claim.item_id == it.id)
            )
            if row is not None:
                session.delete(row)
            touched_items.add(it.code)
            touched_ws.add(ws.code)
            enriched.append(ch)

    # 3) 领用关系联动换版
    for code in sorted(touched_items):
        it = session.scalar(select(Item).where(Item.code == code))
        it.claims_version += 1
        it.updated_at = now
    for code in sorted(touched_ws):
        ws = session.scalar(select(Workstation).where(Workstation.code == code))
        ws.claims_version += 1
        ws.updated_at = now

    return enriched


# ---------------------------------------------------------------- 批次序列化


def batch_to_out(batch: Batch) -> dict:
    involved = None
    if batch.fingerprint:
        involved = {
            "items": sorted(batch.fingerprint.get("items", {}).keys()),
            "workstations": sorted(batch.fingerprint.get("workstations", {}).keys()),
        }
    return {
        "id": batch.id,
        "title": batch.title,
        "status": batch.status,
        "ops": [op.payload for op in batch.ops],
        "violations": batch.violations,
        "involved": involved,
        "created_at": batch.created_at,
        "verified_at": batch.verified_at,
        "published_at": batch.published_at,
    }
