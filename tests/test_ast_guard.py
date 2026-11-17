"""静态安全检查（AST 白名单/黑名单）与技能契约检查。"""

from autoskill.ast_guard import check_code, check_contract


class TestImportWhitelist:
    def test_whitelist_modules_pass(self):
        code = "import json\nimport re\nfrom datetime import date\nfrom collections import OrderedDict\n"
        assert check_code(code).ok

    def test_banned_network_imports(self):
        for mod in ("requests", "urllib.request", "socket", "http.client", "subprocess", "os", "pathlib", "sys"):
            report = check_code(f"import {mod}\n")
            assert not report.ok
            assert any("禁止导入" in v for v in report.violations), mod

    def test_from_import_banned_root(self):
        report = check_code("from urllib.request import urlopen\n")
        assert not report.ok

    def test_relative_import_banned(self):
        report = check_code("from .helpers import thing\n")
        assert not report.ok
        assert any("相对导入" in v for v in report.violations)

    def test_whitelist_is_frozen(self):
        from autoskill.ast_guard import IMPORT_WHITELIST

        banned = {"os", "sys", "subprocess", "socket", "urllib", "requests", "shutil", "pathlib", "io", "contextlib", "importlib", "builtins"}
        assert not (banned & set(IMPORT_WHITELIST))


class TestBuiltinBlacklist:
    def test_eval_exec_open_compile(self):
        for call in ("eval('1')", "exec('x=1')", "open('f.txt')", "compile('1', '<s>', 'eval')", "__import__('json')", "input()", "globals()", "breakpoint()"):
            report = check_code(f"def run(params):\n    return {call}\n")
            assert not report.ok, call
            assert any("禁止调用内置函数" in v for v in report.violations)

    def test_indirect_call_not_flagged_by_name(self):
        # 白名单函数的正常调用不受影响
        report = check_code("import json\n\ndef run(params):\n    return json.loads(params['x'])\n")
        assert report.ok


class TestDunderBan:
    def test_dunder_attribute(self):
        report = check_code("def run(params):\n    return params.__class__\n")
        assert not report.ok
        assert any("双下划线属性" in v for v in report.violations)

    def test_dunder_name(self):
        report = check_code("def run(params):\n    return __builtins__\n")
        assert not report.ok


class TestSyntaxAndContract:
    def test_syntax_error(self):
        report = check_code("def run(params):\n    return (params\n")
        assert not report.ok
        assert "语法错误" in report.violations[0]

    def test_contract_ok(self):
        code = "def run(params):\n    return {'v': params['x']}\n"
        assert check_contract(code).ok

    def test_contract_missing_run(self):
        report = check_contract("def helper(x):\n    return x\n")
        assert not report.ok
        assert any("run(params)" in v for v in report.violations)

    def test_contract_wrong_signature(self):
        report = check_contract("def run(a, b):\n    return a\n")
        assert not report.ok
        assert any("params" in v for v in report.violations)

    def test_contract_run_inside_class_not_top_level(self):
        code = "class W:\n    def run(self, params):\n        return params\n"
        report = check_contract(code)
        assert not report.ok
