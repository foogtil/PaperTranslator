"""Provider-reported usage only; never treat missing usage as a free request."""


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def usage_delta(raw, phase="translation", retry=False):
    raw = raw if isinstance(raw, dict) else {}
    inp = _number(raw.get("prompt_tokens", raw.get("input_tokens")))
    out = _number(raw.get("completion_tokens", raw.get("output_tokens")))
    details_in = raw.get("prompt_tokens_details", raw.get("input_tokens_details")) or {}
    details_out = raw.get("completion_tokens_details", raw.get("output_tokens_details")) or {}
    details_in = details_in if isinstance(details_in, dict) else {}
    details_out = details_out if isinstance(details_out, dict) else {}
    cached = _number(raw.get("prompt_cache_hit_tokens", details_in.get("cached_tokens")))
    reasoning = _number(details_out.get("reasoning_tokens"))
    result = {"input_tokens": inp or 0, "output_tokens": out or 0, "requests": 1,
              "cached_input_tokens": cached or 0, "reasoning_tokens": reasoning or 0,
              "retry_requests": int(retry), "usage_missing_requests": int(inp is None or out is None),
              "cache_reported_requests": int(cached is not None),
              "reasoning_reported_requests": int(reasoning is not None)}
    if phase in ("translation", "vision", "connection"):
        result[phase + "_input_tokens"] = inp or 0
        result[phase + "_output_tokens"] = out or 0
        result[phase + "_requests"] = 1
    return result


def format_usage(usage, tr=lambda value: value):
    n = lambda key: format(_number(usage.get(key)) or 0, ",")
    known = lambda tokens, reports: n(tokens) if usage.get(reports, 0) else "?"
    text = tr("当前文档累计（接口已报告）：输入 {input} · 输出 {output} · 请求 {requests}").format(
        input=n("input_tokens"), output=n("output_tokens"), requests=n("requests"))
    text += "\n" + tr("其中：缓存输入 {cached} · 思考输出 {reasoning} · 重试 {retries} · 用量缺失 {missing}").format(
        cached=known("cached_input_tokens", "cache_reported_requests"),
        reasoning=known("reasoning_tokens", "reasoning_reported_requests"),
        retries=n("retry_requests"), missing=n("usage_missing_requests"))
    def phase_total(phase):
        if usage.get("requests", 0) and phase + "_requests" not in usage:
            return "?"
        return format(sum(_number(usage.get(phase + "_" + k)) or 0 for k in ("input_tokens", "output_tokens")), ",")
    text += "\n" + tr("文字翻译 {text} · 视觉核对 {vision}（输入 + 输出 Token）").format(
        text=phase_total("translation"), vision=phase_total("vision"))
    return text
