"""任务状态谓词 —— 把"状态是否健康 / 完成资格是否成立"收拢成一小组纯函数。

做什么
------
RUNNING_STATUSES：进程重启时要转 interrupted 的运行态集合（持久化层复用）。
required_check_is_fresh：判定一次成功的 run_check 是否还能为"完成"背书。

怎么实现
--------
两个都是纯函数/常量，不碰 IO。evidence_generation 记录"这次成功检查发生在第几次编辑之后"，
edit_generation 记录"当前文件是第几次编辑的产物"。

为什么
------
run_check 只对"它执行那一刻的文件版本"有效。只要后来又发生了 apply_edit，
旧检查的证据代际就落后于当前编辑代际，必须判为失效。
这是"任意成功命令不能替代原始验收"这一安全不变量在代码里的落点。
"""

from __future__ import annotations

from .models import TaskStatus

RUNNING_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.INSPECTING,
        TaskStatus.PLANNING,
        TaskStatus.EDITING,
        TaskStatus.TESTING,
        TaskStatus.REPAIRING,
        TaskStatus.AWAITING_COMMAND_APPROVAL,
    }
)


def required_check_is_fresh(
    verified: bool,
    evidence_generation: int | None,
    edit_generation: int,
) -> bool:
    """A check authorizes completion only for the latest edit generation.

    完成资格三条件缺一不可：
    1. verified —— 原 required_check 确实成功过；
    2. evidence_generation 非空 —— 成功检查的记录存在；
    3. evidence_generation == edit_generation —— 那次检查发生在当前这版文件之后，
       中间没有被任何新编辑覆盖。
    """

    return verified and evidence_generation is not None and evidence_generation == edit_generation
