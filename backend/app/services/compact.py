"""小文件合并的业务规则：执行前容量预估、确认生成结果数据集、批次执行与收益记账。

一次确认只对应一份合并结果数据集：确认前如果同一张表已经有执行中的批次，
直接拒绝，保证并发确认同表任务时只保留一个批次。实际收益只记入真正跑完的
成功批次，失败或仍在执行的分片留在剩余清单里，复核清单不会重复累计。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.compaction import (
    MEASURED_SAVING_RATIO,
    PLANNED_SAVING_RATIO,
    average_output_mb,
    parse_size_mb,
    summarize,
)
from app.store import store

MODULE = "compact"

TASK_STATUS_PENDING = "待确认"
TASK_STATUS_RUNNING = "合并中"
TASK_STATUS_DONE = "已确认"
TASK_STATUS_FAILED = "统计异常"

ACTION_RULES = ["执行合并", "重新预估", "归档任务"]

BATCH_PENDING = "待执行"
BATCH_RUNNING = "执行中"
BATCH_SUCCESS = "成功"
BATCH_FAILED = "失败"

SHARD_PENDING = "待合并"
SHARD_DONE = "已合并"
SHARD_FAILED = "异常"

ACTIVE_BATCH_STATUSES = {BATCH_PENDING, BATCH_RUNNING}
SUCCESS_BATCH_STATUSES = {BATCH_SUCCESS}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class CompactionError(Exception):
    """小文件合并的业务异常，路由层转成可读的失败说明。"""

    def __init__(self, message: str, *, status_code: int = 400, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _seq(rows: list[dict[str, Any]], prefix: str, key: str = "数据集编号") -> str:
    highest = 0
    for row in rows:
        text = str(row.get(key) or "")
        if text.startswith(prefix):
            try:
                highest = max(highest, int(text[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{highest + 1:04d}"


class CompactService:
    # ------------------------------------------------------------------ 列表
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        required = ["任务编号", "目标表名", "文件范围"]
        missing = [field for field in required if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": _next_id(rows)}
        entry.update({field: values.get(field) for field in required})
        entry["status"] = TASK_STATUS_PENDING
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"合并任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于小文件合并可执行范围"
        table = str(entry.get("目标表名"))
        if action == "执行合并":
            batch_no = str(entry.get("批次编号") or "")
            if not batch_no:
                return None, f"任务 {entry.get('任务编号')} 还没有确认生成结果数据集，请先在页面发起确认"
            try:
                self.run_batch(batch_no)
            except CompactionError as error:
                return None, error.message
            return entry, f"合并任务已执行批次 {batch_no}"
        if action == "重新预估":
            try:
                estimate = self.capacity_estimate(table)
            except CompactionError as error:
                return None, error.message
            entry["预计文件数下降"] = estimate["预计文件数下降"]
            entry["预计容量下降MB"] = estimate["预计容量下降MB"]
            return entry, f"已重新预估：预计文件数下降 {estimate['预计文件数下降']}"
        # 归档任务：直接收口状态，不再改动批次收益。
        entry["status"] = TASK_STATUS_DONE
        entry["pending"] = False
        entry["abnormal"] = False
        return entry, "合并任务已归档"

    # -------------------------------------------------------------- 分片统计
    def shard_stats(self, table: str | None = None) -> list[dict[str, Any]]:
        """读取小文件合并任务的现有分片统计，可按目标表过滤。"""
        rows = store.rows("compact_shards")
        if table:
            rows = [row for row in rows if row.get("目标表名") == table]
        return rows

    def _find_task_for_table(self, table: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("目标表名") == table:
                return row
        return None

    def _candidate_shards(self, shards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """候选分片：合并前遗留的小文件，异常分片也算候选（执行时会尝试续跑）。"""
        return [row for row in shards if row.get("状态") != SHARD_DONE]

    def _target_mb(self, task: dict[str, Any]) -> float:
        parsed = parse_size_mb(task.get("目标大小"))
        return float(parsed or 128.0)

    def _parallelism(self, task: dict[str, Any]) -> int:
        try:
            value = int(str(task.get("并行度") or "4").strip() or 4)
        except ValueError:
            value = 4
        return max(value, 1)

    def capacity_estimate(self, table: str) -> dict[str, Any]:
        """执行前的容量预估：读取现有分片统计，按目标大小规划输出文件数。"""
        task = self._find_task_for_table(table)
        if task is None:
            raise CompactionError(f"目标表 {table} 没有对应的合并任务，无法预估容量", status_code=404)
        shards = self._candidate_shards(self.shard_stats(table))
        if not shards:
            raise CompactionError(f"目标表 {table} 没有可合并的小文件分片")
        target_mb = self._target_mb(task)
        sizes = [float(row.get("文件大小MB") or 0.0) for row in shards]
        planned = summarize(sizes, target_mb, PLANNED_SAVING_RATIO)
        partitions: dict[str, int] = {}
        for row in shards:
            path = str(row.get("分区路径") or "未分区")
            partitions[path] = partitions.get(path, 0) + 1
        return {
            "目标表名": table,
            "目标大小": task.get("目标大小") or f"{target_mb:g}MB",
            "目标大小MB": target_mb,
            "并行度": self._parallelism(task),
            "候选分片数": len(shards),
            "分区目录": sorted(partitions),
            "分区分布": partitions,
            "预计文件数下降": planned["file_reduction"],
            "预计容量下降MB": planned["capacity_reduction_mb"],
            "预计输出文件数": planned["output_files"],
            "合并前文件数": planned["input_files"],
            "合并前容量MB": planned["input_mb"],
            "容量回收比例": PLANNED_SAVING_RATIO,
        }

    # ------------------------------------------------------------------ 确认
    def confirm(self, table: str, *, remark: str | None = None) -> dict[str, Any]:
        """页面发起确认：重新预估一次，生成结果数据集与第一个执行批次。

        同一张表已有执行中的批次时拒绝本次确认，保证一次确认只对应一份结果数据集。
        """
        rows = store.rows("compact_batches")
        running = [
            row for row in rows
            if row.get("目标表名") == table and row.get("执行状态") in ACTIVE_BATCH_STATUSES
        ]
        if running:
            raise CompactionError(
                f"目标表 {table} 已有执行中的合并批次 {running[0]['批次编号']}，并发确认只保留一个批次",
                status_code=409,
                detail={"批次编号": running[0]["批次编号"]},
            )

        estimate = self.capacity_estimate(table)
        task = self._find_task_for_table(table)
        assert task is not None  # capacity_estimate 已校验

        batch_seq = len(rows) + 1
        batch_no = f"BATCH-{datetime.now():%Y%m%d}-{batch_seq:03d}"
        batch = {
            "id": _next_id(rows),
            "批次编号": batch_no,
            "目标表名": table,
            "任务编号": task.get("任务编号"),
            "执行状态": BATCH_PENDING,
            "确认时间": _now(),
            "完成时间": None,
            "计划收益": {
                "文件数下降": estimate["预计文件数下降"],
                "容量下降MB": estimate["预计容量下降MB"],
            },
            "实际收益": None,
            "review": False,
        }
        rows.append(batch)

        datasets = store.rows("compact_datasets")
        dataset = {
            "id": _next_id(datasets),
            "数据集编号": _seq(datasets, "CDS-"),
            "表名": table,
            "任务编号": task.get("任务编号"),
            "批次编号": batch_no,
            "目标大小": estimate["目标大小"],
            "目标大小MB": estimate["目标大小MB"],
            "候选分片数": estimate["候选分片数"],
            "分区目录": estimate["分区目录"],
            "预计文件数下降": estimate["预计文件数下降"],
            "预计容量下降MB": estimate["预计容量下降MB"],
            "实际文件数下降": None,
            "实际容量下降MB": None,
            "状态": "待执行",
            "created_at": _now(),
            "remark": remark,
        }
        datasets.append(dataset)

        batch["数据集编号"] = dataset["数据集编号"]

        # 任务转为「待确认」，README 约定任务仍然出现在列表里等待执行。
        task["status"] = TASK_STATUS_PENDING
        task["pending"] = True
        task["abnormal"] = False
        task["批次编号"] = batch_no
        task["数据集编号"] = dataset["数据集编号"]
        task["预计文件数下降"] = estimate["预计文件数下降"]
        task["预计容量下降MB"] = estimate["预计容量下降MB"]

        for shard in self._candidate_shards(self.shard_stats(table)):
            shard["批次编号"] = batch_no
            shard["状态"] = SHARD_PENDING

        store.rows("compact_events").append({
            "id": _next_id(store.rows("compact_events")),
            "目标表名": table,
            "批次编号": batch_no,
            "事件": "确认合并",
            "说明": f"预计文件数下降 {estimate['预计文件数下降']}，预计容量下降 {estimate['预计容量下降MB']}MB",
            "时间": _now(),
        })
        return {"dataset": dataset, "batch": batch, "estimate": estimate}

    # --------------------------------------------------------------- 执行批次
    def run_batch(self, batch_no: str) -> dict[str, Any]:
        """执行一个批次：按并行度分片处理，失败分片留在剩余清单里等待续跑。

        只有整批跑完才把实际收益记入该批次，并同步到结果数据集与复核清单。
        """
        batches = store.rows("compact_batches")
        batch = next((row for row in batches if row.get("批次编号") == batch_no), None)
        if batch is None:
            raise CompactionError(f"合并批次 {batch_no} 不存在", status_code=404)
        if batch.get("执行状态") in SUCCESS_BATCH_STATUSES:
            raise CompactionError(f"合并批次 {batch_no} 已经成功执行，实际收益不会重复累计", status_code=409)
        if batch.get("执行状态") == BATCH_FAILED:
            raise CompactionError(f"合并批次 {batch_no} 已标记失败，请从剩余分片续跑", status_code=409)

        table = str(batch.get("目标表名"))
        task = self._find_task_for_table(table)
        if task is None:
            raise CompactionError(f"目标表 {table} 的合并任务已不存在", status_code=404)

        batch["执行状态"] = BATCH_RUNNING
        task["status"] = TASK_STATUS_RUNNING
        task["pending"] = True
        task["abnormal"] = False
        dataset = next(
            (row for row in store.rows("compact_datasets") if row.get("批次编号") == batch_no),
            None,
        )
        if dataset is not None:
            dataset["状态"] = "执行中"

        shards = [row for row in self.shard_stats(table) if row.get("批次编号") == batch_no]
        pending = [row for row in shards if row.get("状态") == SHARD_PENDING]
        target_mb = float(dataset.get("目标大小MB") if dataset else self._target_mb(task))

        # 并行度决定这一轮处理多少分片；每轮只处理一批，剩余分片留到下一轮。
        round_size = self._parallelism(task)
        round_pending = pending[:round_size]

        processed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for shard in round_pending:
            # 统计异常的分片：标记为异常并留在剩余清单里，从剩余分片继续。
            if shard.get("统计异常"):
                shard["状态"] = SHARD_FAILED
                failed.append(shard)
                continue
            shard["状态"] = SHARD_DONE
            processed.append(shard)

        remaining = [row for row in shards if row.get("状态") in {SHARD_PENDING, SHARD_FAILED}]
        if failed:
            batch["执行状态"] = BATCH_FAILED
            batch["异常分片数"] = len(failed)
            task["status"] = TASK_STATUS_FAILED
            task["pending"] = True
            task["abnormal"] = True
            if dataset is not None:
                dataset["状态"] = "统计异常"
            store.rows("compact_events").append({
                "id": _next_id(store.rows("compact_events")),
                "目标表名": table,
                "批次编号": batch_no,
                "事件": "统计异常",
                "说明": f"{len(failed)} 个分片统计异常，实际收益暂不记账，可从剩余分片续跑",
                "时间": _now(),
            })
            raise CompactionError(
                f"批次 {batch_no} 有 {len(failed)} 个分片统计异常，实际收益未记账，可从剩余分片继续",
                status_code=422,
                detail={"异常分片数": len(failed), "剩余分片数": len(remaining)},
            )

        if remaining:
            # 还有剩余分片：本轮不记账，等全部处理完再一次性记入成功批次。
            batch["执行状态"] = BATCH_PENDING
            batch["已处理分片数"] = len(processed)
            if dataset is not None:
                dataset["状态"] = "执行中"
                dataset["已处理分片数"] = len(processed)
            store.rows("compact_events").append({
                "id": _next_id(store.rows("compact_events")),
                "目标表名": table,
                "批次编号": batch_no,
                "事件": "分段执行",
                "说明": f"本轮合并 {len(processed)} 个分片，剩余 {len(remaining)} 个分片，实际收益待全部完成后再记账",
                "时间": _now(),
            })
            return {
                "batch": batch,
                "dataset": dataset,
                "actual": None,
                "processed": len(processed),
                "remaining": len(remaining),
                "complete": False,
            }

        # 全部合并完成：用已经合并的分片汇总实际收益，写回批次与数据集。
        completed = [row for row in shards if row.get("状态") == SHARD_DONE]
        sizes = [float(row.get("文件大小MB") or 0.0) for row in completed]
        actual = summarize(sizes, target_mb, MEASURED_SAVING_RATIO)
        batch["执行状态"] = BATCH_SUCCESS
        batch["完成时间"] = _now()
        batch["实际收益"] = {
            "文件数下降": actual["file_reduction"],
            "容量下降MB": actual["capacity_reduction_mb"],
        }
        batch["review"] = True

        if dataset is not None:
            dataset["实际文件数下降"] = actual["file_reduction"]
            dataset["实际容量下降MB"] = actual["capacity_reduction_mb"]
            dataset["实际输出文件数"] = actual["output_files"]
            dataset["合并后容量MB"] = actual["merged_mb"]
            dataset["平均输出文件MB"] = average_output_mb(actual)
            dataset["状态"] = "已确认"
            dataset["执行完成时间"] = batch["完成时间"]

        task["status"] = TASK_STATUS_DONE
        task["pending"] = False
        task["abnormal"] = False
        task["执行耗时"] = f"{len(completed)} 个分片已合并"
        task["实际文件数下降"] = actual["file_reduction"]
        task["实际容量下降MB"] = actual["capacity_reduction_mb"]

        store.rows("compact_events").append({
            "id": _next_id(store.rows("compact_events")),
            "目标表名": table,
            "批次编号": batch_no,
            "事件": "批次成功",
            "说明": f"实际文件数下降 {actual['file_reduction']}，实际容量下降 {actual['capacity_reduction_mb']}MB",
            "时间": _now(),
        })
        return {
            "batch": batch,
            "dataset": dataset,
            "actual": actual,
            "processed": len(processed),
            "remaining": 0,
            "complete": True,
        }

    def resume_batch(self, batch_no: str) -> dict[str, Any]:
        """统计异常后从剩余分片继续：把异常分片重新放回待合并，再执行一轮。"""
        batch = next(
            (row for row in self.batches() if row.get("批次编号") == batch_no),
            None,
        )
        if batch is None:
            raise CompactionError(f"合并批次 {batch_no} 不存在", status_code=404)
        if batch.get("执行状态") in SUCCESS_BATCH_STATUSES:
            raise CompactionError(f"合并批次 {batch_no} 已经成功执行，无需续跑", status_code=409)
        reset = 0
        for shard in self.shard_stats(str(batch.get("目标表名"))):
            if shard.get("批次编号") == batch_no and shard.get("状态") == SHARD_FAILED:
                shard["状态"] = SHARD_PENDING
                shard["统计异常"] = False
                reset += 1
        if reset:
            batch["执行状态"] = BATCH_PENDING
            batch.pop("异常分片数", None)
        return self.run_batch(batch_no)

    # ------------------------------------------------------------------ 复核
    def batches(self) -> list[dict[str, Any]]:
        return store.rows("compact_batches")

    def datasets(self, *, table: str | None = None, batch_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows("compact_datasets")
        if table:
            rows = [row for row in rows if row.get("表名") == table]
        if batch_no:
            rows = [row for row in rows if row.get("批次编号") == batch_no]
        return rows

    def dataset_detail(self, dataset_no: str) -> dict[str, Any] | None:
        dataset = next(
            (row for row in store.rows("compact_datasets") if row.get("数据集编号") == dataset_no),
            None,
        )
        if dataset is None:
            return None
        batch_no = dataset.get("批次编号")
        batch = next(
            (row for row in store.rows("compact_batches") if row.get("批次编号") == batch_no),
            None,
        )
        shards = [row for row in self.shard_stats(str(dataset.get("表名"))) if row.get("批次编号") == batch_no]
        versions = [row for row in store.rows("compact_versions") if row.get("数据集编号") == dataset_no]
        return {
            **dataset,
            "批次": batch,
            "分片": shards,
            "分片数": len(shards),
            "版本依据": versions,
        }

    def review_detail(self, dataset_no: str) -> dict[str, Any] | None:
        detail = self.dataset_detail(dataset_no)
        if detail is None:
            return None
        dataset = next(
            (row for row in store.rows("compact_datasets") if row.get("数据集编号") == dataset_no),
            None,
        )
        assert dataset is not None
        batch = detail.get("批次") or {}
        review_rows = [row for row in self.review_rows(batch_no=batch.get("批次编号"))]
        # 同一张表的复核清单只保留一份快照：成功批次优先展示实际收益，否则展示预计收益。
        snapshot = review_rows[0] if review_rows else None
        return {
            "dataset": detail,
            "收益来源": {
                "计划": batch.get("计划收益"),
                "实际": batch.get("实际收益"),
                "统计报告": self.batch_stat_report(str(dataset.get("表名"))),
                "候选分片数": dataset.get("候选分片数"),
                "容量回收比例": MEASURED_SAVING_RATIO,
            },
            "复核清单": snapshot,
            "版本依据": detail.get("版本依据"),
        }

    def batch_stat_report(self, table: str) -> dict[str, Any] | None:
        """统计报告使用同一批小文件合并结果数据集的目标表统计口径。"""
        reports = [row for row in store.rows("stats") if row.get("表名称") == table]
        if not reports:
            return None
        latest = max(reports, key=lambda row: str(row.get("统计时间") or ""))
        return {
            "报告编号": latest.get("报告编号"),
            "表名称": table,
            "文件总数": latest.get("文件总数"),
            "总容量": latest.get("总容量"),
            "统计时间": latest.get("统计时间"),
        }

    # -------------------------------------------------------------- 核对工作台
    def workbench(self, *, table: str | None = None) -> dict[str, Any]:
        """合并收益核对工作台：按批次归组展示数据集、收益与版本依据。"""
        datasets = self.datasets(table=table)
        batches = {row.get("批次编号"): row for row in self.batches()}
        versions = store.rows("compact_versions")
        group_map: dict[str, dict[str, Any]] = {}
        for dataset in datasets:
            batch_no = str(dataset.get("批次编号"))
            group = group_map.setdefault(batch_no, {
                "批次编号": batch_no,
                "目标表名": dataset.get("表名"),
                "执行状态": (batches.get(batch_no) or {}).get("执行状态"),
                "数据集": [],
                "版本依据": [],
                "计划收益": (batches.get(batch_no) or {}).get("计划收益"),
                "实际收益": (batches.get(batch_no) or {}).get("实际收益"),
            })
            group["数据集"].append(dataset)
            group["版本依据"].extend(
                row for row in versions if row.get("数据集编号") == dataset.get("数据集编号")
            )
        groups = sorted(group_map.values(), key=lambda item: str(item.get("批次编号")))
        for group in groups:
            # 同一批次下的重复数据集记录已经合并：一次确认只对应一份合并结果。
            group["数据集记录数"] = len(group["数据集"])
            group["复核清单"] = self.review_rows(batch_no=group["批次编号"])
        dataset_tables = {str(dataset.get("表名")) for dataset in datasets}
        conflict_tables = sorted({
            str(row.get("表名称"))
            for row in store.rows("iceberg")
            if str(row.get("表名称")) in dataset_tables
        })
        return {
            "groups": groups,
            "total": len(groups),
            "冲突": self.conflicts(table=table),
            "同表优化任务": sorted(conflict_tables),
        }

    def conflicts(self, *, table: str | None = None) -> list[dict[str, Any]]:
        """同表优化任务与运行中的合并批次冲突：给出唯一快照口径。"""
        running = [
            row for row in self.batches()
            if row.get("执行状态") in ACTIVE_BATCH_STATUSES and (not table or row.get("目标表名") == table)
        ]
        results: list[dict[str, Any]] = []
        for batch in running:
            name = str(batch.get("目标表名"))
            optimize = [row for row in store.rows("iceberg") if row.get("表名称") == name]
            if not optimize:
                continue
            results.append({
                "目标表名": name,
                "批次编号": batch.get("批次编号"),
                "优化任务": [row.get("任务编号") for row in optimize],
                "快照口径": "成功批次展示实际收益，否则展示预计（分段）收益，复核清单只保留一份完整快照",
            })
        return results

    def sync_version(self, module: str, dataset_no: str, version: str) -> tuple[dict[str, Any] | None, str]:
        """其它业务模块同步版本依据：按数据集编号归入对应批次，重复同步只更新不新增。"""
        dataset = next(
            (row for row in store.rows("compact_datasets") if row.get("数据集编号") == dataset_no),
            None,
        )
        if dataset is None:
            return None, f"结果数据集 {dataset_no} 不存在，版本依据未同步"
        rows = store.rows("compact_versions")
        existing = next(
            (row for row in rows
             if row.get("来源模块") == module and row.get("数据集编号") == dataset_no),
            None,
        )
        if existing is not None:
            existing["版本依据"] = version
            existing["同步时间"] = _now()
            return existing, f"{module} 的版本依据已更新到批次 {dataset.get('批次编号')}"
        record = {
            "id": _next_id(rows),
            "来源模块": module,
            "数据集编号": dataset_no,
            "批次编号": dataset.get("批次编号"),
            "目标表名": dataset.get("表名"),
            "版本依据": version,
            "同步时间": _now(),
        }
        rows.append(record)
        return record, f"{module} 的版本依据已同步到批次 {dataset.get('批次编号')}"

    # -------------------------------------------------------------- 统计报告
    def stats_report(self, *, table: str | None = None) -> dict[str, Any]:
        """统计报告：与核对工作台使用同一批结果数据集，按成功批次列预计与实际收益。

        只统计成功批次的实际收益，失败与执行中的批次只登记预计收益并注明状态，
        因此报告里的合计与复核清单口径一致，不会重复累计。
        """
        datasets = self.datasets(table=table)
        batches = {row.get("批次编号"): row for row in self.batches()}
        lines: list[dict[str, Any]] = []
        totals = {
            "成功批次数": 0,
            "预计文件数下降": 0,
            "预计容量下降MB": 0.0,
            "实际文件数下降": 0,
            "实际容量下降MB": 0.0,
        }
        for dataset in datasets:
            batch_no = str(dataset.get("批次编号"))
            batch = batches.get(batch_no) or {}
            planned = batch.get("计划收益") or {}
            actual = batch.get("实际收益") or {}
            success = batch.get("执行状态") in SUCCESS_BATCH_STATUSES
            line = {
                "数据集编号": dataset.get("数据集编号"),
                "批次编号": batch_no,
                "任务编号": dataset.get("任务编号"),
                "目标表名": dataset.get("表名"),
                "执行状态": batch.get("执行状态"),
                "是否成功批次": success,
                "预计文件数下降": dataset.get("预计文件数下降") or planned.get("文件数下降"),
                "预计容量下降MB": dataset.get("预计容量下降MB") or planned.get("容量下降MB"),
                "实际文件数下降": actual.get("文件数下降") if success else None,
                "实际容量下降MB": actual.get("容量下降MB") if success else None,
            }
            lines.append(line)
            totals["预计文件数下降"] += int(line["预计文件数下降"] or 0)
            totals["预计容量下降MB"] = round(totals["预计容量下降MB"] + float(line["预计容量下降MB"] or 0), 2)
            if success:
                totals["成功批次数"] += 1
                totals["实际文件数下降"] += int(line["实际文件数下降"] or 0)
                totals["实际容量下降MB"] = round(
                    totals["实际容量下降MB"] + float(line["实际容量下降MB"] or 0), 2
                )
        stat_reports = {}
        for name in sorted({str(dataset.get("表名")) for dataset in datasets}):
            report = self.batch_stat_report(name)
            if report is not None:
                # 统计报告使用同一批小文件合并结果数据集，而不是另起一份口径。
                report = {
                    **report,
                    "数据集编号": [
                        dataset.get("数据集编号")
                        for dataset in datasets
                        if dataset.get("表名") == name
                    ],
                    "统计口径": "按小文件合并结果数据集汇总",
                }
            stat_reports[name] = report
        # 统计报告写入后会同步到合并收益核对工作台，两处读的是同一批数据集。
        store.rows("compact_reports").append({
            "id": _next_id(store.rows("compact_reports")),
            "生成时间": _now(),
            "目标表名": table,
            "统计报告": stat_reports,
            "合计": totals,
        })
        return {
            "module": "compact",
            "generated_at": _now(),
            "target_table": table,
            "lines": lines,
            "totals": totals,
            "统计报告": stat_reports,
            "核对工作台": "/api/compact/workbench",
        }

    def review_rows(
        self,
        *,
        table: str | None = None,
        batch_no: str | None = None,
        include_planned: bool = True,
    ) -> list[dict[str, Any]]:
        """复核清单：每个成功批次一行，避免同表预计/实际被重复累计。

        快照只取一份：成功批次展示实际收益；没有成功批次但存在执行中批次的表
        展示预计（分段）收益，并标注进行中；两者互斥，不会同时出现两行。
        """
        batches = store.rows("compact_batches")
        if batch_no:
            batches = [row for row in batches if row.get("批次编号") == batch_no]
        elif table:
            batches = [row for row in batches if row.get("目标表名") == table]

        rows: list[dict[str, Any]] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in batches:
            grouped.setdefault(str(row.get("目标表名")), []).append(row)

        for name, group in grouped.items():
            success = [row for row in group if row.get("执行状态") in SUCCESS_BATCH_STATUSES]
            if success:
                for batch in success:
                    rows.append(self._review_row(name, batch, "实际"))
                continue
            if not include_planned:
                continue
            # 没有成功批次时只展示最近一个批次的预计（分段）收益：失败或执行中都算，
            # 同一张表仍然只有一行，不会把分段收益和实际收益叠在一起。
            latest = max(group, key=lambda row: str(row.get("确认时间") or ""))
            rows.append(self._review_row(name, latest, "预计"))
        return rows

    def _review_row(self, table: str, batch: dict[str, Any], kind: str) -> dict[str, Any]:
        planned = batch.get("计划收益") or {}
        actual = batch.get("实际收益") or {}
        file_gain = actual.get("文件数下降") if kind == "实际" else planned.get("文件数下降")
        capacity_gain = actual.get("容量下降MB") if kind == "实际" else planned.get("容量下降MB")
        return {
            "目标表名": table,
            "批次编号": batch.get("批次编号"),
            "数据集编号": batch.get("数据集编号"),
            "收益类型": kind,
            "执行状态": batch.get("执行状态"),
            "文件数下降": file_gain,
            "容量下降MB": capacity_gain,
            "复核状态": "已核对" if kind == "实际" else "待执行",
        }

    # --------------------------------------------------------------- 导出核对
    def build_manifest(self, table: str | None = None) -> dict[str, Any]:
        """导出合并结果清单文件的内容：批次、数据集与收益逐条对应。"""
        batches = store.rows("compact_batches")
        if table:
            batches = [row for row in batches if row.get("目标表名") == table]
        datasets = {row.get("批次编号"): row for row in store.rows("compact_datasets")}
        entries: list[dict[str, Any]] = []
        for batch in batches:
            dataset = datasets.get(batch.get("批次编号")) or {}
            planned = batch.get("计划收益") or {}
            actual = batch.get("实际收益") or {}
            entries.append({
                "批次编号": batch.get("批次编号"),
                "任务编号": batch.get("任务编号"),
                "目标表名": batch.get("目标表名"),
                "数据集编号": batch.get("数据集编号"),
                "执行状态": batch.get("执行状态"),
                "计划文件数下降": planned.get("文件数下降"),
                "计划容量下降MB": planned.get("容量下降MB"),
                "实际文件数下降": actual.get("文件数下降"),
                "实际容量下降MB": actual.get("容量下降MB"),
            })
        return {
            "module": "compact",
            "generated_at": _now(),
            "target_table": table,
            "total": len(entries),
            "entries": entries,
        }

    def save_export(self, manifest: dict[str, Any], filename: str) -> dict[str, Any]:
        """把每次导出的清单留存下来，下载文件可以随时回看。"""
        rows = store.rows("compact_exports")
        record = {
            "id": _next_id(rows),
            "导出编号": _seq(rows, "EXP-", key="导出编号"),
            "文件名": filename,
            "目标表名": manifest.get("target_table"),
            "批次条数": manifest.get("total"),
            "导出时间": manifest.get("generated_at"),
            "content": manifest,
        }
        rows.append(record)
        return record

    def exports(self, export_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows("compact_exports")
        if export_no:
            return [row for row in rows if row.get("导出编号") == export_no]
        return rows

    def export_content(self, export_no: str) -> dict[str, Any] | None:
        for row in store.rows("compact_exports"):
            if row.get("导出编号") == export_no:
                return row
        return None

    def verify_manifest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """核对下载回来的清单文件：逐批比对任务编号、数据集与预计/实际收益。"""
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            raise CompactionError("清单文件没有可核对的批次记录")
        batches = {str(row.get("批次编号")): row for row in store.rows("compact_batches")}
        datasets = {str(row.get("批次编号")): row for row in store.rows("compact_datasets")}
        results: list[dict[str, Any]] = []
        matched = 0
        for entry in entries:
            batch_no = str(entry.get("批次编号") or "")
            batch = batches.get(batch_no)
            dataset = datasets.get(batch_no)
            issues: list[str] = []
            if batch is None:
                issues.append("平台内找不到该批次")
            else:
                if str(batch.get("任务编号")) != str(entry.get("任务编号")):
                    issues.append("任务编号不一致")
                planned = batch.get("计划收益") or {}
                actual = batch.get("实际收益") or {}
                if planned.get("文件数下降") != entry.get("计划文件数下降"):
                    issues.append("计划文件数下降不一致")
                if actual.get("文件数下降") != entry.get("实际文件数下降"):
                    issues.append("实际文件数下降不一致")
            if dataset is None:
                issues.append("平台内找不到该批次的结果数据集")
            elif str(dataset.get("数据集编号")) != str(entry.get("数据集编号")):
                issues.append("数据集编号不一致")
            if issues:
                results.append({"批次编号": batch_no, "ok": False, "问题": issues})
            else:
                matched += 1
                results.append({"批次编号": batch_no, "ok": True, "问题": []})
        return {
            "module": "compact",
            "total": len(entries),
            "matched": matched,
            "mismatched": len(entries) - matched,
            "results": results,
        }


service = CompactService()
