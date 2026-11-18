"""小文件合并接口：容量预估、确认生成结果数据集、批次执行与合并收益核对工作台。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.schemas import ActionResult, CompactEstimate, EntryPayload, PageResult
from app.services.compact import CompactionError, service

router = APIRouter(prefix="/api/compact", tags=["小文件合并"])

LIST_FIELDS = [
    "任务编号", "目标表名", "文件范围", "合并策略", "目标大小", "并行度", "执行耗时", "任务状态",
]
STATUSES = ["待确认", "合并中", "已确认", "统计异常"]


def _fail(error: CompactionError) -> None:
    raise HTTPException(status_code=error.status_code, detail={"message": error.message, **error.detail})


# ---------------------------------------------------------------- 分片与预估
@router.get("/shard-stats", response_model=dict)
def shard_stats(table: str | None = Query(default=None, description="按目标表名过滤")) -> dict:
    """读取小文件合并任务的现有分片统计，供容量预估与执行批次使用。"""
    rows = service.shard_stats(table)
    summaries: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("目标表名"))
        bucket = summaries.setdefault(name, {"目标表名": name, "分片数": 0, "总容量MB": 0.0, "分区目录": set()})
        bucket["分片数"] += 1
        bucket["总容量MB"] = round(bucket["总容量MB"] + float(row.get("文件大小MB") or 0), 2)
        bucket["分区目录"].add(row.get("分区路径"))
    for bucket in summaries.values():
        bucket["分区目录"] = sorted(bucket["分区目录"])
    return {"items": rows, "total": len(rows), "tables": list(summaries.values())}


@router.get("/estimate", response_model=CompactEstimate)
def estimate(table: str = Query(description="目标表名")) -> dict:
    """执行前容量预估：按目标大小规划合并后的文件数与容量下降。"""
    try:
        return service.capacity_estimate(table)
    except CompactionError as error:
        _fail(error)


@router.post("/confirm", response_model=ActionResult)
def confirm(payload: EntryPayload) -> ActionResult:
    """页面发起确认：生成一份小文件合并结果数据集与一个执行批次。

    同一张表已有执行中的批次时返回 409，并发确认只保留一个批次。
    """
    table = str(payload.values.get("目标表名") or "").strip()
    if not table:
        return ActionResult(ok=False, message="缺少必填字段：目标表名")
    try:
        result = service.confirm(table, remark=payload.remark)
    except CompactionError as error:
        _fail(error)
    dataset = result["dataset"]
    return ActionResult(
        ok=True,
        message=f"已生成结果数据集 {dataset['数据集编号']}，批次 {result['batch']['批次编号']}",
        entry={**dataset, "estimate": result["estimate"], "batch": result["batch"]},
    )


# -------------------------------------------------------------- 批次与数据集
@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    table: str | None = Query(default=None),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """按目标表列出合并批次，成功批次带实际收益，未成功批次只有计划收益。"""
    rows = [row for row in service.batches() if not table or row.get("目标表名") == table]
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.post("/batches/{batch_no}/run", response_model=ActionResult)
def run_batch(batch_no: str) -> ActionResult:
    """执行批次的一轮；统计异常的分片留在剩余清单里，实际收益不记账。"""
    try:
        result = service.run_batch(batch_no)
    except CompactionError as error:
        _fail(error)
    if not result["complete"]:
        return ActionResult(
            ok=True,
            message=(
                f"批次 {batch_no} 本轮合并 {result['processed']} 个分片，"
                f"剩余 {result['remaining']} 个分片；实际收益待全部完成后记账"
            ),
            entry=result,
        )
    actual = result["actual"]
    return ActionResult(
        ok=True,
        message=(
            f"批次 {batch_no} 执行成功，实际文件数下降 {actual['file_reduction']}，"
            f"实际容量下降 {actual['capacity_reduction_mb']}MB"
        ),
        entry=result,
    )


@router.post("/batches/{batch_no}/resume", response_model=ActionResult)
def resume_batch(batch_no: str) -> ActionResult:
    """统计异常后从剩余分片继续，只把异常分片放回待合并，不重复累计已合并分片。"""
    try:
        result = service.resume_batch(batch_no)
    except CompactionError as error:
        _fail(error)
    if result["complete"]:
        message = f"批次 {batch_no} 已从剩余分片续跑完成"
    else:
        message = f"批次 {batch_no} 已续跑一轮，剩余 {result['remaining']} 个分片"
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/datasets", response_model=PageResult[dict])
def list_datasets(
    table: str | None = Query(default=None),
    batch_no: str | None = Query(default=None),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """列出小文件合并结果数据集；同一批次只保留一条记录。"""
    rows = service.datasets(table=table, batch_no=batch_no)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.get("/datasets/{dataset_no}", response_model=dict)
def get_dataset(dataset_no: str) -> dict:
    """结果数据集详情：收益来源、批次、分片与版本依据。"""
    detail = service.dataset_detail(dataset_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"结果数据集 {dataset_no} 不存在")
    return detail


# ------------------------------------------------------------------ 复核详情
@router.get("/review", response_model=dict)
def review(table: str | None = Query(default=None)) -> dict:
    """复核清单：显示预计与实际的文件数量下降和容量下降。"""
    rows = service.review_rows(table=table)
    return {"items": rows, "total": len(rows)}


@router.get("/review/{dataset_no}", response_model=dict)
def review_detail(dataset_no: str) -> dict:
    """从合并列表进入复核详情；重复打开不会新增数据集记录。"""
    detail = service.review_detail(dataset_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"结果数据集 {dataset_no} 不存在")
    return detail


# -------------------------------------------------------------- 统计报告
@router.get("/reports", response_model=dict)
def report(table: str | None = Query(default=None)) -> dict:
    """统计报告：与核对工作台使用同一批结果数据集，按成功批次记录预计与实际收益。"""
    return service.stats_report(table=table)


# -------------------------------------------------------------- 核对工作台
@router.get("/workbench", response_model=dict)
def workbench(table: str | None = Query(default=None)) -> dict:
    """合并收益核对工作台：数据集按批次归组，附上版本依据、复核清单与冲突。"""
    return service.workbench(table=table)


@router.post("/versions", response_model=ActionResult)
def sync_version(payload: EntryPayload) -> ActionResult:
    """其它业务模块消费结果数据集后，把版本依据同步到核对工作台。"""
    values = payload.values
    module = str(values.get("来源模块") or "").strip()
    dataset_no = str(values.get("数据集编号") or "").strip()
    version = str(values.get("版本依据") or "").strip()
    missing = [
        label for label, value in (("来源模块", module), ("数据集编号", dataset_no), ("版本依据", version))
        if not value
    ]
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    record, message = service.sync_version(module, dataset_no, version)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


# ------------------------------------------------------------ 导出与核对文件
def _export_file_response(manifest: dict[str, Any], filename: str) -> Response:
    body = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/file")
def export_file(table: str | None = Query(default=None)) -> Response:
    """把合并结果导出为清单文件，浏览器直接下载，便于事后回看与核对。"""
    manifest = service.build_manifest(table)
    filename = f"compact-results-{datetime.now():%Y%m%d-%H%M%S}.json"
    service.save_export(manifest, filename)
    return _export_file_response(manifest, filename)


@router.get("/exports", response_model=PageResult[dict])
def list_exports(page: int = 1, size: int = 50) -> PageResult[dict]:
    """已导出的清单文件清单，可回看每次下载的内容。"""
    rows = [{key: value for key, value in row.items() if key != "content"} for row in service.exports()]
    rows = list(reversed(rows))
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.get("/exports/{export_no}", response_model=dict)
def get_export(export_no: str) -> dict:
    """回看某次导出的清单文件内容。"""
    record = service.export_content(export_no)
    if record is None:
        raise HTTPException(status_code=404, detail=f"导出文件 {export_no} 不存在")
    return record


@router.get("/exports/{export_no}/download")
def download_export(export_no: str) -> Response:
    """重新下载已留存清单文件，用于和平台批次逐条核对。"""
    record = service.export_content(export_no)
    if record is None:
        raise HTTPException(status_code=404, detail=f"导出文件 {export_no} 不存在")
    return _export_file_response(record["content"], str(record.get("文件名") or f"{export_no}.json"))


@router.post("/verify", response_model=dict)
def verify(payload: dict[str, Any]) -> dict:
    """核对下载回来的清单文件：逐批比对任务编号、数据集与收益。"""
    try:
        return service.verify_manifest(payload)
    except CompactionError as error:
        _fail(error)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出小文件合并清单（接口预览）：返回当前过滤条件下的全量数据。"""
    manifest = service.build_manifest()
    return {"module": "compact", "total": manifest["total"], "items": manifest["entries"]}


# ------------------------------------------------------------------ 任务明细
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="待确认、合并中、已确认、统计异常"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤小文件合并列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条合并任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"合并任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条合并任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="合并任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条合并任务执行执行合并、重新预估、归档任务。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
