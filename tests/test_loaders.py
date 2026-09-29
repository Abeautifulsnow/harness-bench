"""加载层测试：dataset 版本/hash、suite/case 解析、错误路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_eval.errors import InvalidCallError
from agent_eval.loading.loader import (
    load_benchmark,
    load_dataset,
    load_profile,
    load_suites,
    resolve_cases,
    resolve_suites,
)
from agent_eval.models.case import Case

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / "evals"


def test_load_dataset_version_and_hash() -> None:
    info, cases = load_dataset(EVALS, "database-core@1.0.0")
    assert info.version == "1.0.0"
    assert len(cases) >= 6
    assert len(info.hash) == 64
    info2, _ = load_dataset(EVALS, "database-core@latest")
    assert info2.hash == info.hash


def test_load_dataset_version_mismatch() -> None:
    with pytest.raises(InvalidCallError, match="version mismatch"):
        load_dataset(EVALS, "database-core@9.9.9")


def test_load_benchmark_and_suites() -> None:
    b = load_benchmark(EVALS, "database-core")
    assert b.dataset == "database-core@1.0.0"
    suites = load_suites(EVALS)
    assert {"smoke", "core"} <= set(suites)


def test_load_profile_smoke_has_fallbacks() -> None:
    profile = load_profile(EVALS, "smoke")
    ids = {m.id: m for m in profile.metrics}
    assert ids["agent.task_completion"].fallback == "native.output_checks"


def test_resolve_cases_smoke_excludes_negatives() -> None:
    b = load_benchmark(EVALS, "database-core")
    suites = load_suites(EVALS)
    _, cases = load_dataset(EVALS, b.dataset)
    smoke = resolve_cases(b, suites, cases, tag_filter=["smoke"])
    assert all("smoke" in c.tags for c in smoke)
    assert not any("negative" in c.tags for c in smoke)
    # 无 tag 过滤 = benchmark 声明的套件并集（不含只被其它套件选中的 case）
    full = resolve_cases(b, suites, cases)
    in_suites = {
        c.id for name in b.suites for c in cases if any(t in c.tags for t in suites[name].tags)
    }
    assert {c.id for c in full} == in_suites
    assert len(full) < len(cases)  # security / golden 套件不在本 benchmark 的声明里

    security, _counts = resolve_suites(b, suites, cases, ["security"])
    assert {c.id for c in security} & {c.id for c in full} == set()


def test_resolve_unknown_suite() -> None:
    b = load_benchmark(EVALS, "database-core")
    with pytest.raises(InvalidCallError, match="unknown suite"):
        resolve_cases(
            b,
            {},
            [],
        )


def test_duplicate_case_id_rejected(tmp_path: Path) -> None:
    ds = tmp_path / "datasets" / "d1"
    (ds / "cases").mkdir(parents=True)
    (ds / "dataset.yaml").write_text("id: d1\nversion: 1.0.0\n", encoding="utf-8")
    body = Case.model_validate(
        {
            "id": "c1",
            "version": 1,
            "name": "c",
            "input": {"type": "single_turn", "prompt": "hi"},
        }
    ).model_dump()
    import yaml

    for name in ("a.yaml", "b.yaml"):
        (ds / "cases" / name).write_text(yaml.safe_dump(body), encoding="utf-8")
    with pytest.raises(InvalidCallError, match="duplicate case id"):
        load_dataset(tmp_path, "d1")


def test_single_turn_requires_prompt() -> None:
    with pytest.raises(Exception, match="prompt"):
        Case.model_validate({"id": "x", "version": 1, "name": "x", "input": {}})


class TestDefinitionTreeParser:
    """Spec §22.12：换解析器（libyaml）只换实现，不换语义。"""

    def test_parser_matches_yaml_safe_load_on_every_definition_file(self) -> None:
        """对整棵定义树逐文件比对：``_parse_yaml`` 必须与 ``yaml.safe_load`` 同结果。

        这条断言不依赖本机有没有 libyaml——它钉住的正是"两者等价"这件事本身。
        """
        import yaml as yaml_mod

        from agent_eval.loading.loader import _parse_yaml

        files = sorted(EVALS.rglob("*.yaml"))
        assert files, "定义树里应当有 YAML 文件"
        for path in files:
            with path.open("r", encoding="utf-8") as fh:
                assert _parse_yaml(fh) == yaml_mod.safe_load(path.read_text(encoding="utf-8")), path

    def test_definition_loader_refuses_python_object_tags(self, tmp_path: Path) -> None:
        """safe 语义是契约：``!!python/object`` 必须报错，不能构造任意对象。

        这条把"用的是 safe 解析器"变成可执行的断言，而不是靠读代码确认。
        """
        import yaml as yaml_mod

        from agent_eval.loading.loader import _read_yaml

        evil = tmp_path / "evil.yaml"
        evil.write_text(
            "payload: !!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8"
        )
        with pytest.raises(yaml_mod.YAMLError):
            _read_yaml(evil)
