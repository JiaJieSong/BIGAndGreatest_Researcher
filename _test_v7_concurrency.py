# -*- coding: utf-8 -*-
"""V7 并发安全压力测试：注册表锁（Mock，离线）。

    python _test_v7_concurrency.py

覆盖:
- T1  100 并发线程压 SourceRegistry + EvidenceRegistry：source_id /
      version_id / evidence_id 全局唯一（300+ 个 ID 无重复）
- T2  无死锁 / 无丢失（证据数 = 线程数 × 每线程证据数）
- T3  锁的串行行为不变（V5 回归由 _test_v5_citation 另行保证）
"""
# -*- coding: utf-8 -*-
import sys
import threading

import research_v5 as v5

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

N = 100
sreg = v5.SourceRegistry()
ereg = v5.EvidenceRegistry(sreg)
barrier = threading.Barrier(N)
errors = []


def worker(i):
    try:
        barrier.wait()
        url = f"https://site{i}.com/page"
        r = sreg.register(url, title=f"S{i}", page_text=f"正文第 {i} 段。" * 10)
        assert r is not None, f"worker {i}: 来源注册失败"
        sid = r["source_id"]
        vid = r["version_id"]
        for k in range(2):
            ev = ereg.register_from_llm(
                sid, vid, f"正文第 {i} 段。" * 10,
                claim=f"论断 {i}-{k}", llm_text=f"正文第 {i} 段。",
                round_no=1, question_id=f"q{i}")
            assert ev is not None, f"worker {i}: 证据登记失败"
    except Exception as e:
        errors.append((i, repr(e)))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()

assert not errors, f"并发错误: {errors[:5]}"
sids = [s["source_id"] for s in sreg.sources.values()]
vids = [v["version_id"] for s in sreg.sources.values() for v in s["page_versions"]]
eids = [e["evidence_id"] for e in ereg.evidence.values()]

print(f"来源 {len(sreg)} 个 / 版本 {len(vids)} 个 / 证据 {len(ereg)} 条")
assert len(sids) == len(set(sids)), "source_id 有重复!"
assert len(vids) == len(set(vids)), "version_id 有重复!"
assert len(eids) == len(set(eids)), "evidence_id 有重复!"
assert len(eids) == N * 2, f"证据数应为 {N * 2}，实际 {len(eids)}"
print(f"T1+T2+T3 OK: {N} 并发 × (1 来源 + 2 证据) = "
      f"{len(sids)}+{len(vids)}+{len(eids)} 个 ID 全局唯一，无死锁无丢失")

print("\nALL V7 CONCURRENCY TESTS PASSED")
