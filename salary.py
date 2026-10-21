# -*- coding: utf-8 -*-
"""薪资文本解析 → 月薪K数值区间（支持按薪资筛选/排序）
覆盖常见格式：10-15K / 15-30K·15薪 / 20K / 8-10万·年 / 1.5万 / 6千-8千 /
150元·天 / 30元·小时 / 面议(→None)"""
import re

# (min_K, max_K)；无法解析返回 (None, None)
_RANGE_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_k(text):
    if not text:
        return None, None
    t = str(text).strip()
    nums = [float(x) for x in _RANGE_RE.findall(t)]
    if not nums:
        return None, None
    lo, hi = nums[0], (nums[1] if len(nums) > 1 else nums[0])

    if "小时" in t:                       # 时薪 → 月薪K（22天×8h）
        factor = 22 * 8 / 1000
    elif "天" in t:                        # 日薪 → 月薪K（22天）
        factor = 22 / 1000
    elif "年" in t:                        # 年薪 → 月薪K
        if "万" in t:
            factor = 10000 / 12 / 1000
        elif "千" in t:
            factor = 1000 / 12 / 1000
        else:                              # 元/年
            factor = 1 / 12
    elif "万" in t:                        # 万/月
        factor = 10
    elif "千" in t:                        # 千/月 = K
        factor = 1
    elif "元" in t:                        # 元/月
        factor = 1 / 1000
    else:                                  # 默认已是 K
        factor = 1

    lo_k, hi_k = lo * factor, hi * factor
    lo_k, hi_k = round(min(lo_k, hi_k)), round(max(lo_k, hi_k))
    return lo_k, hi_k
