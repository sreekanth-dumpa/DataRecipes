"""Independent oracle: the recipe's definitions implemented in plain Python over
raw source rows, without any compiled SQL.  It is test evidence (fixture
agreement), not proof of business truth."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any


def oracle(rows: dict[str, list[tuple]], intent: dict[str, Any]) -> list[dict[str, Any]]:
    state, lob = intent["state"], intent["lob"]
    cs, ce = date.fromisoformat(intent["cohort_start"]), date.fromisoformat(intent["cohort_end"])
    w = int(intent["window_days"])
    cutoff = datetime.fromisoformat(intent["knowledge_cutoff"].replace("Z", ""))
    dims = intent["dimensions"]
    xwalk = dict(rows["ref.channel_crosswalk"])
    first: dict[str, tuple] = {}
    for (qid, it, st, lb, ch, pv, issue, status, elig, rec) in rows["src_quote.quote_iteration"]:
        if status != "ISSUED" or not elig or lb != lob or st != state or rec > cutoff:
            continue
        cur = first.get(qid)
        if cur is None or (issue, it) < (cur[0], cur[1]):
            first[qid] = (issue, it, pv, st, xwalk.get(ch, "unknown"))
    binds = defaultdict(list)
    for (bid, qid, et, bd, rec) in rows["src_policy.bind_event"]:
        if et == "BIND" and rec <= cutoff:
            binds[qid].append(bd)
    acc: dict[tuple, dict[str, int]] = {}
    for qid, (issue, _it, pv, st, ch) in first.items():
        if not (cs <= issue <= ce):
            continue
        vals = {"product_version": pv, "channel": ch, "state": st}
        key = tuple(vals[d] for d in dims)
        a = acc.setdefault(key, {"eligible": 0, "converted": 0, "immature_excluded": 0, "cohort_quotes": 0})
        a["cohort_quotes"] += 1
        end = issue + timedelta(days=w)
        if end > cutoff.date():
            a["immature_excluded"] += 1
            continue
        a["eligible"] += 1
        if any(issue <= b <= end for b in binds.get(qid, [])):
            a["converted"] += 1
    return [{**dict(zip(dims, k)), **v} for k, v in sorted(acc.items(), key=lambda kv: tuple(map(str, kv[0])))]
