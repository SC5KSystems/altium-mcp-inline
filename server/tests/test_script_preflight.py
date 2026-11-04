"""Offline tests for server/script_preflight.py.

    python -m unittest discover -s server/tests -p "test_script_preflight.py"

The corpus in preflight_corpus/ holds good bodies (calls proven on a live
board) and bad ones, each bad file naming on its first line a text its error
must contain. The dev experiment bodies in dev/*.pas ran in the dev sandbox,
so they double as a check against false positives.
"""

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import script_preflight as sp  # noqa: E402

CORPUS = HERE / "preflight_corpus"


def messages(report, kind="errors"):
    return " | ".join(p["message"] + " " + p.get("suggestion", "") for p in report[kind])


class CorpusTest(unittest.TestCase):
    def test_good_corpus_passes(self):
        for f in sorted((CORPUS / "good").glob("*.pas")):
            with self.subTest(f.name):
                r = sp.check(f.read_text(encoding="utf-8"))
                self.assertTrue(r["ok"], f"{f.name}: {sp.format_report(r)}")

    def test_bad_corpus_fails_with_reason(self):
        bad = sorted((CORPUS / "bad").glob("*.pas"))
        self.assertGreaterEqual(len(bad), 10)
        for f in bad:
            with self.subTest(f.name):
                src = f.read_text(encoding="utf-8")
                expect = src.splitlines()[0].split("expect:", 1)[1].strip()
                r = sp.check(src)
                self.assertFalse(r["ok"], f"{f.name} passed")
                self.assertIn(expect, messages(r), f"{f.name}: {sp.format_report(r)}")

    def test_dev_bodies_pass_in_dev_sandbox(self):
        bodies = sorted((sp.REPO_DIR / "dev").glob("*.pas"))
        self.assertTrue(bodies)
        for f in bodies:
            with self.subTest(f.name):
                r = sp.check(f.read_text(encoding="utf-8"), sp.DEV_SANDBOX)
                self.assertTrue(r["ok"], f"{f.name}: {sp.format_report(r)}")


class RuleTest(unittest.TestCase):
    def test_result_is_an_error_even_when_unknowns_are_allowed(self):
        r = sp.check("Result := 'x';", allow_unknown=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["errors"][0]["suggestion"], "ResultText")

    def test_allow_unknown_downgrades_unknown_names(self):
        body = "Obj1 := PCBServer.GetPCBBoardByPath('a');\nI1 := Obj1.SomeNewApiCall;"
        self.assertFalse(sp.check(body)["ok"])
        r = sp.check(body, allow_unknown=True)
        self.assertTrue(r["ok"])
        self.assertIn("SomeNewApiCall", messages(r, "warnings"))

    def test_allow_unknown_does_not_excuse_list_before_create(self):
        r = sp.check("List1.Add('a');", allow_unknown=True)
        self.assertFalse(r["ok"])

    def test_list_after_create_is_fine(self):
        r = sp.check("List1 := TStringList.Create;\nList1.Add('a');\nResultText := List1.Text;")
        self.assertTrue(r["ok"], sp.format_report(r))

    def test_case_insensitive(self):
        r = sp.check("obj1 := pcbserver.getpcbboardbypath('a');\nsandboxlog('x');")
        self.assertTrue(r["ok"], sp.format_report(r))

    def test_strings_and_comments_are_not_checked(self):
        body = ("SandboxLog('Result := Log(x) Board');  // Board Result\n"
                "{ Rotation3DX } (* GetCurrentPCBBoard *)\nResultText := 'ok';")
        r = sp.check(body)
        self.assertTrue(r["ok"], sp.format_report(r))

    def test_nearest_name_for_misspelled_logger(self):
        r = sp.check("SandboxLgo('x');")
        self.assertEqual(r["errors"][0]["suggestion"].split(", ")[0], "SandboxLog")

    def test_end_without_begin(self):
        r = sp.check("SandboxLog('x');\nend;")
        self.assertIn("without a matching", messages(r))

    def test_with_block_members(self):
        body = ("Obj1 := PCBServer.GetPCBBoardByPath('a');\n"
                "with Obj1 do\nbegin\n    SandboxLog(FileName);\nend;")
        r = sp.check(body)
        self.assertTrue(r["ok"], sp.format_report(r))

    def test_variable_from_dev_sandbox_is_not_in_server_sandbox(self):
        r = sp.check("S4 := 'x';")
        self.assertFalse(r["ok"])
        self.assertIn("dev/sandbox/Sandbox.pas", messages(r))
        self.assertIn("server/SandboxScript/Sandbox.pas", messages(r))
        self.assertTrue(sp.check("S4 := 'x';", sp.DEV_SANDBOX)["ok"])


class WarningTest(unittest.TestCase):
    def warn(self, body):
        r = sp.check(body)
        self.assertTrue(r["ok"], sp.format_report(r))
        return messages(r, "warnings")

    def test_component_without_incomponent(self):
        self.assertIn("InComponent", self.warn("S1 := Obj1.Component.Name.Text;"))

    def test_net_assignment_without_member_registration(self):
        self.assertIn("AddPCBObject", self.warn("Obj1.Net := Obj2;"))

    def test_rebuild_after_outline_edit_without_nudge(self):
        w = self.warn("Obj1.PointCount := 4;\nObj1.Rebuild;")
        self.assertIn("MoveByXY", w)
        r = sp.check("Obj1.PointCount := 4;\nObj1.MoveByXY(1, 0);\nObj1.MoveByXY(-1, 0);\nObj1.Rebuild;")
        self.assertEqual(r["warnings"], [])

    def test_remove_inside_iterator(self):
        body = ("Obj2 := Obj1.BoardIterator_Create;\nObj3 := Obj2.FirstPCBObject;\n"
                "Obj1.RemovePCBObject(Obj3);\nObj1.BoardIterator_Destroy(Obj2);")
        self.assertIn("TInterfaceList", self.warn(body))

    def test_show_message(self):
        self.assertIn("modal", self.warn("ShowMessage('x');"))


class TokenizerTest(unittest.TestCase):
    def test_tokens(self):
        toks = sp.tokenize("x := 1.5e3 + $FF; s := 'it''s' + #13#10; r := 1..3;")
        kinds = [(t.kind, t.text) for t in toks]
        self.assertIn(("number", "1.5e3"), kinds)
        self.assertIn(("number", "$FF"), kinds)
        self.assertIn(("string", "'it''s'"), kinds)
        self.assertIn(("char", "#13"), kinds)
        self.assertIn(("op", ".."), kinds)

    def test_unterminated_comment(self):
        with self.assertRaises(sp.PreflightError):
            sp.tokenize("x := 1; { never closed")

    def test_declarations(self):
        toks = sp.tokenize("procedure P(var A, B: Integer; const C: String);\n"
                           "var X, Y : TStringList; Z : Integer;\nconst K = 3;\n"
                           "type TKind = (kOne, kTwo);\nbegin end;")
        d = sp.declarations(toks)
        for name, kind in (("p", "routine"), ("a", "param"), ("b", "param"), ("c", "param"),
                           ("x", "var"), ("y", "var"), ("z", "var"), ("k", "const"),
                           ("tkind", "type"), ("kone", "const")):
            self.assertEqual(d[name][1], kind, name)
        self.assertEqual(d["x"][2], "tstringlist")


if __name__ == "__main__":
    unittest.main()
