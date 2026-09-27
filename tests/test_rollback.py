"""rollback-preview：前缀解包回退预演测试（备份路径 + 有序回退步骤）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/rollback-preview", json=payload)


def _rollback(existing, incoming, stop_after):
    r = _post(
        {"existing": existing, "incoming": incoming, "stop_after": stop_after}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _steps(body):
    return [
        (
            s["order"],
            s["operation"],
            s["node_type"],
            s["source"],
            s["index"],
            s["canonical_path"],
        )
        for s in body["steps"]
    ]


# ---------------------------------------------------------------------------
# 三阶段回退步骤与备份路径
# ---------------------------------------------------------------------------


def test_three_phase_rollback_steps():
    body = _rollback(
        existing=[
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "assets", "type": "directory"},
        ],
        incoming=[
            {"path": "docs/readme.txt", "type": "file"},  # 覆盖现存文件 → 备份+恢复
            {"path": "assets/logo.png", "type": "file"},  # 新建（assets 已存在）
            {"path": "new/deep/file.bin", "type": "file"},  # 新建 + 隐含新目录 new、new/deep
            {"path": "new", "type": "directory"},  # 复用隐含目录 new → 无回退步骤
            {"path": "tmp", "type": "directory"},  # 新建目录
        ],
        stop_after=5,
    )

    assert body["backup_paths"] == ["docs/readme.txt"]
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 2, "assets/logo.png"),
        (2, "DELETE_FILE", "file", "incoming", 3, "new/deep/file.bin"),
        (3, "RESTORE_FILE", "file", "existing", 1, "docs/readme.txt"),
        # 深 → 浅：new/deep 在 new 之前；new 出生为 #3 推导的隐含目录
        # （#4 仅 REUSE_DIRECTORY 不改变出生来源）；tmp 与 new 同深度按比较键。
        (4, "DELETE_DIRECTORY", "directory", "implicit", None, "new/deep"),
        (5, "DELETE_DIRECTORY", "directory", "implicit", None, "new"),
        (6, "DELETE_DIRECTORY", "directory", "incoming", 5, "tmp"),
    ]
    assert body["summary"] == {
        "total": 5,
        "create": 3,
        "overwrite": 1,
        "reuse_directory": 1,
        "blocked": 0,
        "simulated": 5,
        "backup_files": 1,
        "delete_files": 2,
        "restore_files": 1,
        "delete_directories": 3,
    }


def test_stop_after_zero_simulates_nothing():
    body = _rollback(
        existing=[{"path": "a.txt", "type": "file"}],
        incoming=[
            {"path": "a.txt", "type": "file"},
            {"path": "b.txt", "type": "file"},
        ],
        stop_after=0,
    )
    assert body["entries"] == []
    assert body["backup_paths"] == []
    assert body["steps"] == []
    assert body["summary"]["simulated"] == 0
    assert body["summary"]["total"] == 0


def test_stop_after_prefix_excludes_suffix():
    # 前 1 条覆盖现存文件；第 2 条新建文件落在前缀之外，不产生备份/回退。
    body = _rollback(
        existing=[{"path": "a.txt", "type": "file"}],
        incoming=[
            {"path": "a.txt", "type": "file"},
            {"path": "b.txt", "type": "file"},
        ],
        stop_after=1,
    )
    assert [e["index"] for e in body["entries"]] == [1]
    assert body["backup_paths"] == ["a.txt"]
    assert _steps(body) == [
        (1, "RESTORE_FILE", "file", "existing", 1, "a.txt"),
    ]
    assert body["summary"]["simulated"] == 1


def test_suffix_blocked_entry_not_simulated():
    # stop_after=1 时第 2 条（即使是被阻挡条目）不参与模拟。
    body = _rollback(
        existing=[{"path": "a", "type": "file"}],
        incoming=[
            {"path": "new.txt", "type": "file"},
            {"path": "a/x.txt", "type": "file"},  # 文件祖先阻挡（不模拟）
        ],
        stop_after=1,
    )
    assert body["summary"]["blocked"] == 0
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 1, "new.txt"),
    ]


# ---------------------------------------------------------------------------
# 重复覆盖与新建后覆盖
# ---------------------------------------------------------------------------


def test_repeated_overwrite_same_existing_file_backup_once_restore_once():
    body = _rollback(
        existing=[{"path": "a.txt", "type": "file"}],
        incoming=[
            {"path": "a.txt", "type": "file"},  # 覆盖现存文件
            {"path": "a.txt", "type": "file"},  # 再覆盖一次（恢复目标仍是解包前状态）
            {"path": "a.txt", "type": "file"},  # 第三次
        ],
        stop_after=3,
    )
    assert body["backup_paths"] == ["a.txt"]
    assert _steps(body) == [
        (1, "RESTORE_FILE", "file", "existing", 1, "a.txt"),
    ]
    assert body["summary"]["backup_files"] == 1
    assert body["summary"]["restore_files"] == 1
    assert body["summary"]["delete_files"] == 0


def test_overwrite_newly_created_file_only_deleted():
    body = _rollback(
        existing=[],
        incoming=[
            {"path": "a.txt", "type": "file"},  # 新建
            {"path": "a.txt", "type": "file"},  # 覆盖前缀新建文件：无需备份/恢复
        ],
        stop_after=2,
    )
    assert body["backup_paths"] == []
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 1, "a.txt"),
    ]


def test_created_file_then_overwrite_after_prefix_boundary():
    # 新建在前缀内，覆盖在前缀外：回退仍只是删除该新建文件。
    body = _rollback(
        existing=[],
        incoming=[
            {"path": "a.txt", "type": "file"},
            {"path": "a.txt", "type": "file"},
        ],
        stop_after=1,
    )
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 1, "a.txt"),
    ]


def test_existing_file_overwritten_then_created_back_by_later_prefix_entry():
    # 现存文件先被覆盖（文件），同键无法再变为目录（类型冲突会 BLOCKED），
    # 故同键现存文件一旦覆盖就始终是文件；这里验证覆盖一次即恢复一次。
    body = _rollback(
        existing=[{"path": "a.txt", "type": "file"}],
        incoming=[
            {"path": "a.txt", "type": "file"},
            {"path": "a.txt", "type": "directory"},  # 类型冲突，被阻挡
        ],
        stop_after=2,
    )
    statuses = [e["status"] for e in body["entries"]]
    assert statuses == ["OVERWRITE", "BLOCKED"]
    assert body["backup_paths"] == ["a.txt"]
    assert _steps(body) == [
        (1, "RESTORE_FILE", "file", "existing", 1, "a.txt"),
    ]


# ---------------------------------------------------------------------------
# 被阻挡条目与复用目录不产生回退步骤
# ---------------------------------------------------------------------------


def test_blocked_entries_produce_no_rollback_steps():
    body = _rollback(
        existing=[{"path": "a", "type": "file"}, {"path": "docs", "type": "directory"}],
        incoming=[
            {"path": "../escape.bin", "type": "file"},  # UNSAFE_PATH
            {"path": "a/x.txt", "type": "file"},  # FILE_ANCESTOR
            {"path": "docs", "type": "file"},  # TYPE_CONFLICT
        ],
        stop_after=3,
    )
    assert {e["blocked_reason"] for e in body["entries"]} == {
        "UNSAFE_PATH",
        "FILE_ANCESTOR",
        "TYPE_CONFLICT",
    }
    assert body["backup_paths"] == []
    assert body["steps"] == []
    assert body["summary"] == {
        "total": 3,
        "create": 0,
        "overwrite": 0,
        "reuse_directory": 0,
        "blocked": 3,
        "simulated": 3,
        "backup_files": 0,
        "delete_files": 0,
        "restore_files": 0,
        "delete_directories": 0,
    }


def test_reuse_existing_directory_no_step_and_no_directory_deletion():
    # 现存深层文件推导出 docs（隐含，解包前已有）：复用目录不产生回退步骤，
    # 仅前缀新建的子目录 docs/sub 需删除。
    body = _rollback(
        existing=[{"path": "docs/old.txt", "type": "file"}],
        incoming=[
            {"path": "docs", "type": "directory"},  # 复用现存隐含目录
            {"path": "docs/sub", "type": "directory"},  # 新建
            {"path": "docs/sub/x.txt", "type": "file"},  # 新建
        ],
        stop_after=3,
    )
    assert [e["status"] for e in body["entries"]] == [
        "REUSE_DIRECTORY",
        "CREATE",
        "CREATE",
    ]
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 3, "docs/sub/x.txt"),
        (2, "DELETE_DIRECTORY", "directory", "incoming", 2, "docs/sub"),
    ]


def test_blocked_entry_derives_no_implicit_parent():
    # 被阻挡的新建文件不会推导隐含父目录，回退不得删除任何目录。
    body = _rollback(
        existing=[{"path": "a", "type": "file"}],
        incoming=[{"path": "a/x/y.txt", "type": "file"}],
        stop_after=1,
    )
    assert body["entries"][0]["status"] == "BLOCKED"
    assert body["steps"] == []


# ---------------------------------------------------------------------------
# 目录出生来源：显式目录 vs 隐含父目录（含后被 REUSE 接管）
# ---------------------------------------------------------------------------


def test_implicit_directory_birth_tracked_even_when_reused_later():
    body = _rollback(
        existing=[],
        incoming=[
            {"path": "x/y.txt", "type": "file"},  # 推导 x（隐含）
            {"path": "x", "type": "directory"},  # 复用并接管 x，但 x 仍是前缀新建
        ],
        stop_after=2,
    )
    assert [e["status"] for e in body["entries"]] == ["CREATE", "REUSE_DIRECTORY"]
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 1, "x/y.txt"),
        (2, "DELETE_DIRECTORY", "directory", "implicit", None, "x"),
    ]


def test_existing_implicit_directory_is_not_deleted():
    # 现存深层文件推导出 a、a/b（隐含，解包前已有）：前缀只新建 a/b/c.txt。
    body = _rollback(
        existing=[{"path": "a/b/existing.txt", "type": "file"}],
        incoming=[
            {"path": "a/b/c.txt", "type": "file"},
            {"path": "a", "type": "directory"},  # 复用现存隐含目录
            {"path": "a/b", "type": "directory"},  # 复用现存隐含目录
        ],
        stop_after=3,
    )
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 1, "a/b/c.txt"),
    ]


def test_explicit_new_directory_birth_incoming_source():
    body = _rollback(
        existing=[],
        incoming=[
            {"path": "d", "type": "directory"},
            {"path": "d/e.txt", "type": "file"},
        ],
        stop_after=2,
    )
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 2, "d/e.txt"),
        (2, "DELETE_DIRECTORY", "directory", "incoming", 1, "d"),
    ]


def test_directory_deletion_deep_to_shallow_key_sorted():
    # 深 → 浅；同深度按比较键排序（a 在 z 之前）。
    body = _rollback(
        existing=[],
        incoming=[
            {"path": "z/f.txt", "type": "file"},
            {"path": "a/b/f.txt", "type": "file"},
        ],
        stop_after=2,
    )
    dirs = [
        (s["canonical_path"], s["source"], s["index"])
        for s in body["steps"]
        if s["operation"] == "DELETE_DIRECTORY"
    ]
    assert dirs == [
        ("a/b", "implicit", None),
        ("a", "implicit", None),
        ("z", "implicit", None),
    ]


# ---------------------------------------------------------------------------
# 模拟前缀内的成功条目立即影响后续判定
# ---------------------------------------------------------------------------


def test_prefix_simulation_tree_state_feeds_later_entries():
    body = _rollback(
        existing=[],
        incoming=[
            {"path": "a", "type": "file"},  # 新建文件
            {"path": "a/x.txt", "type": "file"},  # 被 #1 文件祖先阻挡
            {"path": "a/x.txt", "type": "file"},  # 仍被阻挡
        ],
        stop_after=3,
    )
    assert [e["status"] for e in body["entries"]] == [
        "CREATE",
        "BLOCKED",
        "BLOCKED",
    ]
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 1, "a"),
    ]


def test_incoming_overwrite_becomes_blocker_for_later_entry():
    body = _rollback(
        existing=[{"path": "a", "type": "file"}],
        incoming=[
            {"path": "a", "type": "file"},  # 覆盖现存文件
            {"path": "a/x", "type": "file"},  # 被 incoming #1 阻挡
        ],
        stop_after=2,
    )
    assert body["entries"][1]["blocked_by"] == {
        "source": "incoming",
        "index": 1,
        "canonical_path": "a",
    }
    assert body["backup_paths"] == ["a"]
    assert _steps(body) == [
        (1, "RESTORE_FILE", "file", "existing", 1, "a"),
    ]


# ---------------------------------------------------------------------------
# Windows 规范化与现存清单校验（422）沿用
# ---------------------------------------------------------------------------


def test_windows_normalization_shared_backup_path_canonical():
    body = _rollback(
        existing=[{"path": "Docs\\ReadMe.txt", "type": "file"}],
        incoming=[{"path": "docs/readme.TXT", "type": "file"}],
        stop_after=1,
    )
    entry = body["entries"][0]
    assert entry["status"] == "OVERWRITE"
    assert entry["comparison_key"] == "docs/readme.txt"
    # 备份/恢复路径取解包前现存清单的展示形态。
    assert body["backup_paths"] == ["Docs/ReadMe.txt"]
    assert body["steps"][0]["canonical_path"] == "Docs/ReadMe.txt"


def test_backup_paths_ordered_by_first_overwrite_position():
    body = _rollback(
        existing=[
            {"path": "b.txt", "type": "file"},
            {"path": "a.txt", "type": "file"},
        ],
        incoming=[
            {"path": "b.txt", "type": "file"},  # 先覆盖 b
            {"path": "a.txt", "type": "file"},  # 再覆盖 a
        ],
        stop_after=2,
    )
    assert body["backup_paths"] == ["b.txt", "a.txt"]
    assert [
        s["canonical_path"] for s in body["steps"] if s["operation"] == "RESTORE_FILE"
    ] == ["b.txt", "a.txt"]


def test_existing_validation_reused_422():
    # 不安全现存路径
    assert _post(
        {
            "existing": [{"path": "../escape", "type": "file"}],
            "incoming": [{"path": "a.txt", "type": "file"}],
            "stop_after": 1,
        }
    ).status_code == 422
    # 现存同键重复
    assert _post(
        {
            "existing": [
                {"path": "a", "type": "file"},
                {"path": "A", "type": "file"},
            ],
            "incoming": [{"path": "a.txt", "type": "file"}],
            "stop_after": 1,
        }
    ).status_code == 422
    # 现存文件占据祖先路径
    assert _post(
        {
            "existing": [
                {"path": "a/b", "type": "file"},
                {"path": "a/b/c.txt", "type": "file"},
            ],
            "incoming": [{"path": "a.txt", "type": "file"}],
            "stop_after": 1,
        }
    ).status_code == 422


def test_stop_after_validation():
    base = {
        "existing": [],
        "incoming": [
            {"path": "a.txt", "type": "file"},
            {"path": "b.txt", "type": "file"},
        ],
    }
    # 负数
    r = _post({**base, "stop_after": -1})
    assert r.status_code == 422
    # 超过清单长度
    r = _post({**base, "stop_after": 3})
    assert r.status_code == 422
    # 缺失
    r = _post(base)
    assert r.status_code == 422
    # 边界值合法
    r = _post({**base, "stop_after": 2})
    assert r.status_code == 200


def test_incoming_nonempty_required():
    r = _post({"existing": [], "incoming": [], "stop_after": 0})
    assert r.status_code == 422


def test_full_prefix_with_blocked_directory_conflict_keeps_existing_dir():
    # 现存目录被同名文件条目冲突阻挡：目录不是前缀变化，不产生删除步骤。
    body = _rollback(
        existing=[{"path": "docs", "type": "directory"}],
        incoming=[
            {"path": "docs", "type": "file"},  # BLOCKED TYPE_CONFLICT
            {"path": "docs/new.txt", "type": "file"},  # 目录仍在，新建
        ],
        stop_after=2,
    )
    assert [e["status"] for e in body["entries"]] == ["BLOCKED", "CREATE"]
    assert _steps(body) == [
        (1, "DELETE_FILE", "file", "incoming", 2, "docs/new.txt"),
    ]
