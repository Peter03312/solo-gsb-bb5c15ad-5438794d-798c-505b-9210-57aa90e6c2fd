"""前缀解包回退预演：仅模拟解包清单前 ``stop_after`` 条，生成执行前备份路径
与有序回退步骤（纯内存，不访问文件系统，不读取实际文件内容）。

阻挡/覆盖判定完全沿用 :mod:`app.overlay`（不安全路径、文件祖先阻挡、同键
类型冲突、文件覆盖、目录复用），成功条目同样立即写入虚拟目录树并影响后续
条目的判定。

回退步骤可把该前缀造成的变化**还原为解包前状态**，按三个阶段排列：

1. ``DELETE_FILE``：前缀**新建**的文件每个删除一次（之后被同键条目重复
   覆盖仍是同一个新文件，只删一次）；
2. ``RESTORE_FILE``：被覆盖的**解包前现存文件**每个从备份恢复一次；同一路径
   被重复覆盖时恢复目标恒为解包前状态，故只恢复一次（备份清单同键只列一次）；
3. ``DELETE_DIRECTORY``：前缀**新建目录与推导的隐含父目录**在子项处理完后
   由深到浅删除（同深度按 Windows 比较键排序）。

``REUSE_DIRECTORY`` 与 ``BLOCKED`` 条目不产生变化，故不产生回退步骤；解包前
已存在（含现存清单推导出的隐含目录）的目录一律保留。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum

from .normalize import NormalizedEntry
from .overlay import (
    DIR_TYPE,
    FILE_TYPE,
    OverlayDecision,
    OverlayNodeSource,
    OverlayStatus,
    apply_entry,
    build_existing_tree,
)


class RollbackOperation(str, Enum):
    DELETE_FILE = "DELETE_FILE"
    RESTORE_FILE = "RESTORE_FILE"
    DELETE_DIRECTORY = "DELETE_DIRECTORY"


@dataclass(frozen=True)
class RollbackStep:
    """单步回退操作（``order`` 从 1 开始）。"""

    order: int
    operation: RollbackOperation
    node_type: str  # "file" | "directory"
    source: OverlayNodeSource
    index: int | None  # 来源清单下标（从 1 开始）；隐含目录为 None
    canonical_path: str
    comparison_key: str


@dataclass(frozen=True)
class _DirBirth:
    """前缀新建目录在其首次出现（出生）时的来源信息。"""

    source: OverlayNodeSource
    index: int | None
    canonical_path: str


@dataclass(frozen=True)
class PrefixRollbackPlan:
    """前缀模拟结果：逐条目判定、执行前备份路径与有序回退步骤。"""

    decisions: list[tuple[NormalizedEntry, OverlayDecision]]
    backup_paths: list[str]
    steps: list[RollbackStep]


def _depth(key: str) -> int:
    return key.count("/") + 1


def plan_rollback_prefix(
    existing: list[NormalizedEntry],
    incoming: list[NormalizedEntry],
    stop_after: int,
) -> PrefixRollbackPlan:
    """模拟 ``incoming`` 的前 ``stop_after`` 条并生成回退计划。

    调用方保证 ``existing`` 已通过
    :func:`app.overlay.validate_existing_entries` 校验，且
    ``0 <= stop_after <= len(incoming)``。
    """
    tree = build_existing_tree(existing)

    # 解包前现存文件快照：覆盖会就地改写节点的 source/index/canonical_path，
    # 故恢复步骤所需的现存下标与展示路径必须在模拟前记录。
    existing_files: dict[str, tuple[int | None, str]] = {
        key: (node.index, node.canonical_path)
        for key, node in tree.items()
        if node.kind == FILE_TYPE
    }

    decisions: list[tuple[NormalizedEntry, OverlayDecision]] = []
    # 前缀新建文件（按下发顺序；同键只可能出生一次）。
    created_files: list[tuple[str, int, str]] = []
    # 前缀新建目录（含隐含父目录），仅在键首次出现时记录出生信息。
    dir_births: dict[str, _DirBirth] = {}
    # 现存文件首次被覆盖时的前缀位置（备份/恢复按此排序并对同键去重）。
    first_overwrite: dict[str, int] = {}

    for position, entry in enumerate(incoming[:stop_after], start=1):
        before_keys = set(tree)
        decision = apply_entry(tree, entry)
        decisions.append((entry, decision))

        key = entry.comparison_key
        if decision.status is OverlayStatus.CREATE:
            # 仅 CREATE 会向树中添加键：条目自身键 + 新推导的隐含父目录。
            for added_key in set(tree) - before_keys:
                if added_key == key:
                    if entry.entry_type == FILE_TYPE:
                        created_files.append(
                            (key, entry.index, entry.canonical_path)
                        )
                    else:
                        dir_births[key] = _DirBirth(
                            OverlayNodeSource.INCOMING,
                            entry.index,
                            entry.canonical_path,
                        )
                else:
                    # 新推导的父目录必为隐含目录；此时记录的即其出生路径。
                    dir_births[added_key] = _DirBirth(
                        OverlayNodeSource.IMPLICIT,
                        None,
                        tree[added_key].canonical_path,
                    )
        elif decision.status is OverlayStatus.OVERWRITE:
            # 覆盖解包前现存文件才需备份/恢复；覆盖前缀新建文件只需最后删除。
            if key in existing_files and key not in first_overwrite:
                first_overwrite[key] = position

    # 备份路径（同键只列一次，按首次覆盖顺序）。
    overwrite_order = [
        key
        for key, _position in sorted(first_overwrite.items(), key=lambda item: item[1])
    ]
    backup_paths = [existing_files[key][1] for key in overwrite_order]

    planned: list[RollbackStep] = []

    # 1) 新建文件删除一次（按解包顺序）。
    for key, index, canonical_path in created_files:
        planned.append(
            RollbackStep(
                order=0,
                operation=RollbackOperation.DELETE_FILE,
                node_type=FILE_TYPE,
                source=OverlayNodeSource.INCOMING,
                index=index,
                canonical_path=canonical_path,
                comparison_key=key,
            )
        )

    # 2) 被覆盖的现存文件从备份恢复一次（按首次覆盖顺序，恢复目标为解包前状态）。
    for key in overwrite_order:
        index, canonical_path = existing_files[key]
        planned.append(
            RollbackStep(
                order=0,
                operation=RollbackOperation.RESTORE_FILE,
                node_type=FILE_TYPE,
                source=OverlayNodeSource.EXISTING,
                index=index,
                canonical_path=canonical_path,
                comparison_key=key,
            )
        )

    # 3) 新建及隐含父目录在子项处理后由深到浅删除（同深度按比较键排序）。
    for key in sorted(dir_births, key=lambda k: (-_depth(k), k)):
        birth = dir_births[key]
        planned.append(
            RollbackStep(
                order=0,
                operation=RollbackOperation.DELETE_DIRECTORY,
                node_type=DIR_TYPE,
                source=birth.source,
                index=birth.index,
                canonical_path=birth.canonical_path,
                comparison_key=key,
            )
        )

    steps = [
        dataclasses.replace(step, order=order)
        for order, step in enumerate(planned, start=1)
    ]
    return PrefixRollbackPlan(
        decisions=decisions, backup_paths=backup_paths, steps=steps
    )
