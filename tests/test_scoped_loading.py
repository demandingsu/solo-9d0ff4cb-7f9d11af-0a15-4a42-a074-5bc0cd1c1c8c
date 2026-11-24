"""按批次取数自测:读入范围只覆盖相关工位/项目/领用,且不随无关数据增长。"""

from app.db import SessionLocal
from app import services

# ---------------------------------------------------------------- 工具


def make_batch(client, ops, title=""):
    r = client.post("/batches", json={"title": title, "ops": ops})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def verify(client, bid):
    r = client.post(f"/batches/{bid}/verify")
    assert r.status_code == 200, r.text
    return r.json()


def publish(client, bid):
    return client.post(f"/batches/{bid}/publish")


def publish_ok(client, bid):
    r = publish(client, bid)
    assert r.status_code == 200, r.text
    return r.json()


def upsert_item(code, nominal, upper, lower, name="检验项目"):
    return {
        "op": "upsert_item",
        "item_code": code,
        "name": name,
        "nominal_um": nominal,
        "upper_um": upper,
        "lower_um": lower,
    }


def upsert_ws(code, gen, name="工位"):
    return {"op": "upsert_workstation", "workstation_code": code, "name": name, "terminal_generation": gen}


def claim(ws, item):
    return {"op": "claim_item", "workstation_code": ws, "item_code": item}


def unclaim(ws, item):
    return {"op": "unclaim_item", "workstation_code": ws, "item_code": item}


ITEM_SYM = upsert_item("ITEM-SYM", 10000, 10005, 9995, "对称项目")      # ±5
ITEM_ASYM = upsert_item("ITEM-ASYM", 20000, 20008, 19996, "非对称项目")  # +8/-4
WS_NEW = upsert_ws("WS-NEW", "new", "新终端工位")
WS_OLD = upsert_ws("WS-OLD", "old", "旧终端工位")


def setup_base(client):
    bid = make_batch(
        client,
        [
            ITEM_SYM,
            ITEM_ASYM,
            WS_NEW,
            WS_OLD,
            claim("WS-NEW", "ITEM-SYM"),
            claim("WS-NEW", "ITEM-ASYM"),
            claim("WS-OLD", "ITEM-SYM"),
        ],
        title="基础数据",
    )
    assert verify(client, bid)["status"] == "verified"
    publish_ok(client, bid)


def measure_load(ops):
    """直接取数并返回(状态, 读入行数统计)。"""
    with SessionLocal() as session:
        state = services.load_state_for_ops(session, ops)
        return state, state.stats


# ---------------------------------------------------------------- 取数范围


def test_read_scope_does_not_grow_with_unrelated_data(client):
    """只改一项公差时,无关数据再多,读入行数也不变。"""
    setup_base(client)
    ops = [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")]

    state_before, stats_before = measure_load(ops)
    # 相关工位:WS-NEW / WS-OLD(都领用 ITEM-SYM);预览带出 ITEM-ASYM;领用 3 条
    assert set(state_before.workstations) == {"WS-NEW", "WS-OLD"}
    assert set(state_before.items) == {"ITEM-SYM", "ITEM-ASYM"}
    assert stats_before.workstation_rows == 2
    assert stats_before.item_rows == 2
    assert stats_before.claim_rows == 3
    assert stats_before.total_rows == 7

    # 灌入大量无关数据:50 个项目 + 50 个工位 + 50 条领用
    unrelated_ops = []
    for i in range(50):
        unrelated_ops.append(upsert_item(f"OTHER-{i:02d}", 1000 + i, 1002 + i, 998 + i, "无关项目"))
        unrelated_ops.append(upsert_ws(f"WS-OTHER-{i:02d}", "new", "无关工位"))
        unrelated_ops.append(claim(f"WS-OTHER-{i:02d}", f"OTHER-{i:02d}"))
    bid_u = make_batch(client, unrelated_ops, "无关数据")
    assert verify(client, bid_u)["status"] == "verified"
    publish_ok(client, bid_u)

    state_after, stats_after = measure_load(ops)
    assert set(state_after.workstations) == {"WS-NEW", "WS-OLD"}
    assert set(state_after.items) == {"ITEM-SYM", "ITEM-ASYM"}
    # 读入行数完全不变
    assert (stats_after.workstation_rows, stats_after.item_rows, stats_after.claim_rows) == (
        stats_before.workstation_rows,
        stats_before.item_rows,
        stats_before.claim_rows,
    )
    assert stats_after.total_rows == stats_before.total_rows == 7
    # 语句数也固定(点名单查询 + 领用所涉项目的工位 + 相关领用 + 相关项目)
    assert stats_after.queries == stats_before.queries


def test_shared_items_read_once(client):
    """两个相关工位共用同一项目时,项目行只读入一次。"""
    setup_base(client)
    _, stats = measure_load([upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")])
    # ITEM-SYM 同时被 WS-NEW / WS-OLD 领用,仍只计 1 行
    assert stats.item_rows == 2
    assert stats.claim_rows == 3


def test_unrelated_records_skipped_at_query(client):
    setup_base(client)
    # 造一批无关工位/项目
    bid_u = make_batch(
        client,
        [upsert_item("ITEM-X", 5000, 5002, 4998, "无关"), upsert_ws("WS-X", "new", "无关"),
         claim("WS-X", "ITEM-X")],
    )
    verify(client, bid_u)
    publish_ok(client, bid_u)

    state, stats = measure_load([upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")])
    assert "ITEM-X" not in state.items
    assert "WS-X" not in state.workstations
    assert ("WS-X", "ITEM-X") not in state.claims


# ---------------------------------------------------------------- 批末领用关系


def test_old_terminal_workstation_not_named_by_batch(client):
    """批次只改项目,没点名旧工位;批末该工位仍领用非对称项目 → 退回。"""
    setup_base(client)
    bid = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9993, "对称项目")])  # +6/-7
    res = verify(client, bid)
    assert res["status"] == "rejected"
    assert [(v["workstation_code"], v["item_code"]) for v in res["violations"]] == [
        ("WS-OLD", "ITEM-SYM")
    ]


def test_same_batch_add_then_remove_claim_is_legal(client):
    """同批先领用非对称项目再取消:只看批末领用关系 → 核对通过。"""
    setup_base(client)
    bid = make_batch(
        client,
        [claim("WS-OLD", "ITEM-ASYM"), unclaim("WS-OLD", "ITEM-ASYM")],
        "增删领用",
    )
    assert verify(client, bid)["status"] == "verified"
    publish_ok(client, bid)
    old_spec = client.get("/workstations/WS-OLD/spec").json()
    assert [i["item_code"] for i in old_spec["items"]] == ["ITEM-SYM"]


def test_same_batch_remove_then_add_claim_is_rejected(client):
    """同批先取消对称项目再领用非对称项目:批末违规 → 退回。"""
    setup_base(client)
    bid = make_batch(
        client,
        [unclaim("WS-OLD", "ITEM-SYM"), claim("WS-OLD", "ITEM-ASYM")],
        "先删后增",
    )
    res = verify(client, bid)
    assert res["status"] == "rejected"
    assert [(v["workstation_code"], v["item_code"]) for v in res["violations"]] == [
        ("WS-OLD", "ITEM-ASYM")
    ]


def test_same_batch_remove_claim_clears_existing_violation(client):
    """批次取消旧工位对非对称项目的领用 → 批末不再违规。"""
    setup_base(client)
    bid = make_batch(client, [unclaim("WS-OLD", "ITEM-SYM"), claim("WS-OLD", "ITEM-ASYM")])
    assert verify(client, bid)["status"] == "rejected"
    # 改成只取消(不再领用非对称):通过
    r = client.put(f"/batches/{bid}/ops", json={"ops": [unclaim("WS-OLD", "ITEM-SYM")]})
    assert r.status_code == 200
    assert verify(client, bid)["status"] == "verified"
    publish_ok(client, bid)
    assert client.get("/workstations/WS-OLD/spec").json()["items"] == []


# ---------------------------------------------------------------- 预览与指纹范围


def test_preview_lists_all_items_of_relevant_workstations(client):
    setup_base(client)
    bid = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")])
    res = verify(client, bid)
    preview = {p["workstation_code"]: p for p in res["preview"]}
    assert set(preview) == {"WS-NEW", "WS-OLD"}
    assert [i["item_code"] for i in preview["WS-NEW"]["items"]] == ["ITEM-ASYM", "ITEM-SYM"]
    assert [i["item_code"] for i in preview["WS-OLD"]["items"]] == ["ITEM-SYM"]
    sym = next(i for i in preview["WS-NEW"]["items"] if i["item_code"] == "ITEM-SYM")
    assert (sym["upper_um"], sym["lower_um"]) == (10006, 9994)
    # ITEM-ASYM 不是本批所涉,但作为相关工位领用的项目仍完整列出
    asym = next(i for i in preview["WS-NEW"]["items"] if i["item_code"] == "ITEM-ASYM")
    assert asym["symmetric"] is False


def test_preview_only_item_does_not_expand_scope_or_fingerprint(client):
    """只为补齐预览读入的项目:不带入它的其他领用工位,也不进入指纹。"""
    # 基础数据 + ITEM-EXTRA 被 WS-NEW 与 WS-EXTRA 领用
    bid0 = make_batch(
        client,
        [ITEM_SYM, ITEM_ASYM, WS_NEW, WS_OLD,
         upsert_item("ITEM-EXTRA", 30000, 30005, 29995, "预览项目"),
         upsert_ws("WS-EXTRA", "new", "别的工位"),
         claim("WS-NEW", "ITEM-SYM"), claim("WS-NEW", "ITEM-ASYM"),
         claim("WS-OLD", "ITEM-SYM"),
         claim("WS-NEW", "ITEM-EXTRA"), claim("WS-EXTRA", "ITEM-EXTRA")],
    )
    assert verify(client, bid0)["status"] == "verified"
    publish_ok(client, bid0)

    bid_a = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")], "A")
    res = verify(client, bid_a)
    # ITEM-EXTRA 因 WS-NEW 领用而进入预览;但 WS-EXTRA 不因此被纳入预览
    assert {p["workstation_code"] for p in res["preview"]} == {"WS-NEW", "WS-OLD"}
    # 指纹只记录批次点名的项目,不含仅用于预览的 ITEM-EXTRA
    involved = client.get(f"/batches/{bid_a}").json()["involved"]
    assert involved["items"] == ["ITEM-SYM"]
    assert involved["workstations"] == []

    # 核对之后另一个批次改了 ITEM-EXTRA:不应使 A 的核对失效(失效范围不变)
    bid_b = make_batch(client, [upsert_item("ITEM-EXTRA", 30000, 30008, 29996, "预览项目")], "B")
    assert verify(client, bid_b)["status"] == "verified"
    publish_ok(client, bid_b)

    r = publish(client, bid_a)
    assert r.status_code == 200, r.text
    assert client.get("/items/ITEM-SYM").json()["upper_um"] == 10006


def test_publish_failure_still_serves_current_spec(client):
    """发布因核对失效失败时,继续下发当前已生效规范。"""
    setup_base(client)
    bid_a = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")], "A")
    bid_b = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10007, 9993, "对称项目")], "B")
    assert verify(client, bid_a)["status"] == "verified"
    assert verify(client, bid_b)["status"] == "verified"
    publish_ok(client, bid_b)

    r = publish(client, bid_a)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "verification_stale"
    # 现行规范仍是 B 的版本,未被 A 失败影响
    spec = client.get("/workstations/WS-NEW/spec").json()
    sym = next(i for i in spec["items"] if i["item_code"] == "ITEM-SYM")
    assert (sym["upper_um"], sym["lower_um"]) == (10007, 9993)
    assert client.get(f"/batches/{bid_a}").json()["status"] == "draft"
