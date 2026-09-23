#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""即梦提示词随机组合生成器（含一致性校验与自动重抽）。

按维度随机抽取条目，用自然语言模板重写成一段可直接粘贴进即梦的中文提示词。
`-hl` 控制严谨度：0 完全不检查（纯随机），10 最严谨（题材硬过滤 + 大候选量 + 多轮重抽）。

用法:
    python generate.py "一只羊"
    python generate.py "修仙者" -hl 10                     # 最严谨
    python generate.py "修仙者" -hl 0                       # 最松，纯随机
    python generate.py "一个东方美女" -n 5 --camera --seed 42
    python generate.py "一只羊" --raw                       # 逗号拼接的原始形态
    python generate.py "一只羊" -n 3 --json                 # 结构化输出
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = SKILL_DIR / "data"

# 参与随机抽取的维度顺序（character 由用户输入的主体替代）
PICK_KEYS = ["style", "lens", "scene", "detail", "tone", "culture"]

DEFAULT_HL = 5

# 紧凑简写 -> 标准参数：h8 -> -hl 8，n3 -> -n 3，抽3 -> -n 3
COMPACT_RULES = (
    (re.compile(r"^-?hl(\d{1,2})$"), lambda m: ["-hl", m.group(1)]),
    (re.compile(r"^-?h(\d{1,2})$"), lambda m: ["-hl", m.group(1)]),
    (re.compile(r"^-?n(\d{1,3})$"), lambda m: ["-n", m.group(1)]),
    (re.compile(r"^抽(\d{1,3})次?$"), lambda m: ["-n", m.group(1)]),
    (re.compile(r"^(\d{1,3})$"), lambda m: ["-n", m.group(1)]),  # 独立出现的数字视为条数
)


def normalize_argv(argv):
    """把紧凑写法展开成标准参数，例如 h8 n3 修仙者 -> -hl 8 -n 3 修仙者。"""
    expanded = []
    for token in argv:
        for pattern, convert in COMPACT_RULES:
            match = pattern.match(token)
            if match:
                expanded.extend(convert(match))
                break
        else:
            expanded.append(token)
    return expanded


def load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_level(hl: int, count: int, limit: int, candidates_arg, retries_arg) -> dict:
    """把严谨度等级 0-10 翻译成具体的抽卡参数。

    0    纯随机，不做任何检查
    1-2  只按冲突打分选最优，不做题材剔除，候选量与重抽轮数较低
    3-7  开启题材硬过滤，候选量与重抽轮数随等级线性增长
    8-10 最严谨：硬过滤 + 大候选量 + 最多 10 轮重抽，直到出现协调组合
    """
    hl = max(0, min(10, hl))
    if hl == 0:
        return {"hl": 0, "enabled": False, "hard_filter": False, "candidates": count, "retries": 1}

    candidates = candidates_arg if candidates_arg else hl * 12
    retries = retries_arg if retries_arg else hl
    return {
        "hl": hl,
        "enabled": True,
        "hard_filter": hl >= 3,
        "candidates": max(count, min(candidates, limit)),
        "retries": max(1, retries),
    }


def themes_of(text: str, themes: dict) -> set:
    """返回文本命中的主题簇集合。"""
    return {name for name, keywords in themes.items() if any(kw in text for kw in keywords)}


class Judge:
    """一致性裁判：给一组取值打冲突分，分数越低越协调。"""

    def __init__(self, rules):
        rules = rules or {}
        self.themes = rules.get("themes", {})
        self.pairs = {frozenset(pair) for pair in rules.get("conflicts", [])}
        weights = rules.get("weights", {})
        self.subject_weight = weights.get("subject", 3)
        self.default_weight = weights.get("default", 1)
        self.subject_affinity = weights.get("subject_affinity", 0)
        self.hints = rules.get("subject_hints", {})

    @property
    def enabled(self) -> bool:
        return bool(self.themes and self.pairs)

    def tag(self, key: str, value: str) -> set:
        if not value:
            return set()
        if key == "subject":
            hinted = themes_of(value, self.hints)
            return hinted or themes_of(value, self.themes)
        return themes_of(value, self.themes)

    def penalty(self, values: dict) -> int:
        if not self.enabled:
            return 0
        tagged = [(key, self.tag(key, val)) for key, val in values.items() if val]
        total = 0
        for i in range(len(tagged)):
            for j in range(i + 1, len(tagged)):
                is_subject = "subject" in (tagged[i][0], tagged[j][0])
                weight = self.subject_weight if is_subject else self.default_weight
                hits = sum(
                    1
                    for a in tagged[i][1]
                    for b in tagged[j][1]
                    if frozenset((a, b)) in self.pairs
                )
                total += weight * hits
                if is_subject and self.subject_affinity:
                    total += self.subject_affinity * len(tagged[i][1] & tagged[j][1])
        return total

    def conflicts_with(self, tags_a: set, tags_b: set) -> bool:
        return any(frozenset((a, b)) in self.pairs for a in tags_a for b in tags_b)

    def restrict_pools(
        self, pools: dict, subject: str, minimum: int = 8, protected=("lens", "tone")
    ) -> dict:
        """按主体题材做硬过滤：剔除与主体题材直接互斥的维度取值。

        镜头与色调默认不过滤（它们大多中性，跨题材也不会读起来别扭）。
        若过滤后某维度剩余条目过少，则回退原始池，避免抽不出足够方案。
        """
        if not self.enabled:
            return pools
        subject_tags = self.tag("subject", subject)
        if not subject_tags:
            return pools
        restricted = {}
        for key, pool in pools.items():
            if key in protected:
                restricted[key] = pool
                continue
            kept = [v for v in pool if not self.conflicts_with(subject_tags, self.tag(key, v))]
            restricted[key] = kept if len(kept) >= minimum else pool
        return restricted


def render(values: dict, templates: dict, rng: random.Random, raw: bool, with_suffix: bool) -> str:
    """把各维度取值渲染成一段自然语言提示词。"""
    order = templates.get("order") or PICK_KEYS

    if raw:
        parts = [values[key] for key in order if values.get(key)]
        text = "，".join(parts)
    else:
        fragments = []
        for key in order:
            value = values.get(key)
            if not value:
                continue
            patterns = templates.get("slots", {}).get(key)
            fragment = rng.choice(patterns).format(v=value) if patterns else value
            fragment = fragment.strip(" ，,。.;；")
            if fragment:
                fragments.append(fragment)
        text = "，".join(fragments)

    if with_suffix and not raw:
        suffix = (templates.get("suffix") or "").strip(" ，,。")
        if suffix:
            text = f"{text}，{suffix}"

    return text.strip(" ，,。.;；") + "。"


def main() -> int:
    parser = argparse.ArgumentParser(description="即梦提示词随机组合生成器")
    parser.add_argument(
        "subject",
        nargs="?",
        default=None,
        help="画面主体，如：一只羊 / 一个东方美女；省略时自动从「人物与服饰」维度随机抽取",
    )
    parser.add_argument("-n", "--count", type=int, default=1, help="生成条数（默认 1）")
    parser.add_argument("--camera", action="store_true", help="额外随机抽取一条运镜（默认关闭）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子，用于复现同一组组合")
    parser.add_argument("--raw", action="store_true", help="输出逗号拼接的原始形态")
    parser.add_argument("--no-suffix", action="store_true", help="不追加画质后缀")
    parser.add_argument("--json", action="store_true", help="以 JSON 结构化输出")
    parser.add_argument(
        "-hl",
        "--hl",
        type=int,
        default=DEFAULT_HL,
        help="严谨度等级 0-10，默认 5：0 不检查纯随机，10 最严谨（题材硬过滤 + 大候选量 + 多轮重抽）",
    )
    parser.add_argument(
        "--candidates",
        type=int,
        default=None,
        help="每轮抽取的候选组合数，给值后覆盖 -hl 的推导值",
    )
    parser.add_argument("--retries", type=int, default=None, help="重抽轮数，给值后覆盖 -hl 的推导值")
    parser.add_argument(
        "--no-consistency",
        action="store_true",
        help="关闭一致性校验，等价 -hl 0（纯随机，会出现题材矛盾）",
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="数据目录")
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    data_dir = Path(args.data_dir)
    jimeng = load_json(data_dir / "jimeng_prompts.json")
    templates = load_json(data_dir / "templates.json")
    if not jimeng or not templates:
        sys.exit(f"缺少数据文件，请先执行: python {SKILL_DIR / 'scripts' / 'build_data.py'}")

    if args.count < 1:
        sys.exit("生成条数必须 >= 1")

    labels = {dim["key"]: dim["label"] for dim in jimeng["dimensions"]}
    pools = {
        dim["key"]: dim["items"]
        for dim in jimeng["dimensions"]
        if dim["key"] in PICK_KEYS and dim["items"]
    }
    missing = [key for key in PICK_KEYS if key not in pools]
    if missing:
        sys.exit(f"数据缺少维度: {', '.join(missing)}，请重新执行 build_data.py")

    # 未提供主体时，从「人物与服饰」维度随机抽取作为主体
    character_pool = next(
        (
            dim["items"]
            for dim in jimeng["dimensions"]
            if dim["key"] == "character" and dim["items"]
        ),
        [],
    )
    auto_subject = args.subject is None
    if auto_subject and not character_pool:
        sys.exit("未提供画面主体，且数据中缺少「人物与服饰」维度，无法自动抽取主体")

    base_limit = min(len(pool) for pool in pools.values())
    if args.count > base_limit:
        sys.exit(f"生成条数 {args.count} 超过单维度候选上限 {base_limit}，请降低数量")

    hl = 0 if args.no_consistency else args.hl
    settings = resolve_level(hl, args.count, base_limit, args.candidates, args.retries)

    rng = random.Random(args.seed)
    judge = Judge(None if not settings["enabled"] else load_json(data_dir / "consistency.json"))

    camera_pool = []
    if args.camera:
        camera_pool = (load_json(data_dir / "camera_moves.json") or {}).get("items", [])
        if not camera_pool:
            sys.exit("运镜数据为空，请重新执行 build_data.py")
        if args.count > len(camera_pool):
            sys.exit(f"运镜候选仅 {len(camera_pool)} 条，不足以支撑 {args.count} 条互不重复的输出")

    # 题材硬过滤：先把与主体题材互斥的取值踢出候选池，从源头减少矛盾组合
    if settings["hard_filter"] and not auto_subject:
        pools = judge.restrict_pools(pools, args.subject, minimum=max(8, args.count))

    limit = min(len(pool) for pool in pools.values())
    if args.count > limit:
        sys.exit(f"生成条数 {args.count} 超过当前严谨度下可用候选上限 {limit}，请降低数量或调低 -hl")

    candidates = max(args.count, min(settings["candidates"], limit))
    if auto_subject:
        candidates = min(candidates, len(character_pool))
    rounds = settings["retries"]

    scored = []
    for _ in range(rounds):
        picks = {key: rng.sample(pool, candidates) for key, pool in pools.items()}
        subject_picks = (
            rng.sample(character_pool, candidates) if auto_subject else [args.subject] * candidates
        )
        if args.camera:
            take = min(candidates, len(camera_pool))
            camera_picks = rng.sample(camera_pool, take) + [None] * (candidates - take)
        else:
            camera_picks = [None] * candidates

        for index in range(candidates):
            values = {"subject": subject_picks[index]}
            for key in PICK_KEYS:
                values[key] = picks[key][index]
            if camera_picks[index]:
                values["camera"] = camera_picks[index]
            scored.append((judge.penalty(values), values))

        if judge.enabled and scored and min(item[0] for item in scored) <= 0:
            break

    scored.sort(key=lambda item: item[0])
    selected = [values for _, values in scored[: args.count]]

    results = []
    for values in selected:
        prompt = render(values, templates, rng, args.raw, not args.no_suffix)
        results.append({"prompt": prompt, "penalty": judge.penalty(values), "values": values})

    total_candidates = candidates * rounds
    worst = max(item["penalty"] for item in results) if results else 0

    if args.json:
        payload = {
            "subject": args.subject,
            "auto_subject": auto_subject,
            "count": args.count,
            "camera": args.camera,
            "seed": args.seed,
            "hl": settings["hl"],
            "settings": settings,
            "evaluated": total_candidates,
            "results": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if auto_subject:
        print("【主体】未提供主体，已随机取自「人物与服饰」维度（同批次内不重复）。")
        print()

    for index, item in enumerate(results, start=1):
        if args.count > 1:
            print(f"方案 {index}/{args.count}")
        print(f"【提示词】{item['prompt']}")
        subject_label = "随机主体" if auto_subject else "主体"
        pairs = [f"人物与服饰={item['values']['subject']}（{subject_label}）"]
        for key in PICK_KEYS:
            pairs.append(f"{labels.get(key, key)}={item['values'][key]}")
        if item["values"].get("camera"):
            pairs.append(f"运镜={item['values']['camera']}")
        print("【维度拆解】" + " | ".join(pairs))
        if judge.enabled:
            print(
                f"【一致性】严谨度 hl={settings['hl']}/10 · 协调评分 {item['penalty']}"
                f"（从 {total_candidates} 组候选中筛选，分越低越协调）"
            )
        if index != len(results):
            print()

    if judge.enabled and worst > 0:
        print(
            f"\n提示：本轮未找到完全协调的组合（最佳评分 {min(item['penalty'] for item in results)}），"
            f"可提高 -hl（当前 {settings['hl']}）或换一个更具体主体后重试。"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
