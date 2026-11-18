"""小文件合并的容量预估与收益计算。

规划阶段读取分片的现有大小，按目标大小做顺序装箱，得到预计的输出文件数；
执行阶段用同一套装箱口径统计已经合并完成的分片，得到实际收益。预计与实际
使用不同的容量回收比例：规划取保守值，执行取实测值。所有金额都按 MB 计算，
统一保留两位小数，方便复核清单直接比对。
"""
from __future__ import annotations

import math

PLANNED_SAVING_RATIO = 0.05
MEASURED_SAVING_RATIO = 0.06


def parse_size_mb(value: object, default: float | None = 128.0) -> float | None:
    """把「128MB」「1.5GB」这类目标大小解析成 MB；无法识别时返回默认值。"""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else default
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return default
    multiplier = 1.0
    for suffix, factor in (("TB", 1024 * 1024.0), ("GB", 1024.0), ("MB", 1.0), ("KB", 1 / 1024.0)):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            multiplier = factor
            break
    try:
        number = float(text)
    except ValueError:
        return default
    if number <= 0:
        return default
    return round(number * multiplier, 4)


def pack_bins(sizes_mb: list[float], target_mb: float) -> list[float]:
    """按目标大小顺序装箱：累计到再放一个就会超出目标时收口。"""
    if target_mb <= 0:
        return [round(sum(sizes_mb), 2)] if sizes_mb else []
    bins: list[float] = []
    current = 0.0
    has_file = False
    for size in sizes_mb:
        if has_file and current + size > target_mb:
            bins.append(round(current, 2))
            current = 0.0
            has_file = False
        current += float(size)
        has_file = True
    if has_file:
        bins.append(round(current, 2))
    return bins


def summarize(sizes_mb: list[float], target_mb: float, saving_ratio: float) -> dict[str, float | int]:
    """汇总一组分片的文件数量下降与容量下降。"""
    input_files = len(sizes_mb)
    input_mb = round(sum(float(size) for size in sizes_mb), 2)
    bins = pack_bins(sizes_mb, target_mb)
    output_files = len(bins)
    capacity_reduction_mb = round(input_mb * saving_ratio, 2)
    merged_mb = round(input_mb - capacity_reduction_mb, 2)
    return {
        "input_files": input_files,
        "input_mb": input_mb,
        "output_files": output_files,
        "file_reduction": max(input_files - output_files, 0),
        "capacity_reduction_mb": capacity_reduction_mb,
        "merged_mb": merged_mb,
        "saving_ratio": saving_ratio,
        "target_mb": round(float(target_mb), 2),
    }


def average_output_mb(summary: dict[str, float | int]) -> float:
    """输出文件的平均大小，用于导出清单里描述合并后的文件。"""
    output_files = int(summary.get("output_files") or 0)
    if output_files <= 0:
        return 0.0
    return round(float(summary.get("merged_mb") or 0.0) / output_files, 2)


def chunks(items: list[object], size: int) -> list[list[object]]:
    """按并行度切分待处理分片；并行度小于 1 时按单组处理。"""
    width = max(int(size or 1), 1)
    return [items[index:index + width] for index in range(0, len(items), width)]


def ceil_div(value: float, divisor: float) -> int:
    return int(math.ceil(value / divisor)) if divisor else 0
