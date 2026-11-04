"""Net-membership audit: a live sandbox script that finds primitives whose
Net disagrees with their net's member group.

    python -m offline membership-audit --board-path "C:\\boards\\x.PcbDoc" [-o audit.pas]
        [--fix] [--limit 200]

Altium keeps two records of a primitive's net: the primitive's own Net and
the net's member group (what Net.GroupIterator walks). Setting Obj.Net alone
does not put the primitive in the group; connectivity and DRC read the
group, so a primitive in the wrong group (or none) can hide an unrouted net
from DRC. Neither record is in the saved file the way it is in memory, so
the offline reader cannot see this: it has to run live.

The script reports, per primitive (tracks, arcs, vias, pads, fills,
regions): "stale member" (in a net's group but its Net is another net or
none) and "unregistered" (its Net is set but it is not in that net's
group). With --fix it removes stale members from the group they do not
belong to and adds every primitive to its own net's group. Run it with
run_altium_script; the board must be open. UNVERIFIED: no Altium here.
"""

import json

from .live import LiveError, SANDBOX, Script, _json, q

KINDS = "eTrackObject, eArcObject, eViaObject, ePadObject, eFillObject, eRegionObject"


def _finding(s, kind, group_expr, limit):
    """Append one finding to S1 (JSON objects, comma separated), at most limit."""
    s(f"if B1 < {int(limit)} then")
    s.block()
    s("if B1 > 0 then S1 := S1 + ',';",
      "if Obj3.Net = nil then S2 := '' else S2 := Obj3.Net.Name;",
      f"S1 := S1 + '{{\"kind\": \"{kind}\", \"object\": \"' + Obj3.ObjectIDString + "
      f"'\", \"layer\": ' + IntToStr(Obj3.Layer) + ', \"group\": \"' + {group_expr} + "
      "'\", \"net\": \"' + S2 + '\"}';",
      "B1 := B1 + 1;")
    s.close()


def membership_script(board_path, fix=False, limit=200):
    """The script body (str). Its ResultText is JSON: checked, stale_members,
    unregistered, fixed, findings (at most `limit`)."""
    s = Script("net membership audit" + (" and fix" if fix else ""), proven=False,
               notes=["UNVERIFIED: written without Altium; run on a scratch copy first"])
    s("SandboxLog('membership audit');",
      f"Obj1 := PCBServer.GetPCBBoardByPath({q(board_path)});",
      "if Obj1 = nil then",
      f"    ResultText := '{{\"error\": \"board is not open in Altium: {_json(board_path)}\"}}'",
      "else")
    s.block()
    s("List1 := TStringList.Create;",
      "I1 := 0;", "I2 := 0;", "I3 := 0;", "B1 := 0;", "S1 := '';", "S3 := '';")
    if fix:
        s("PCBServer.PreProcess;")
    # pass 1: every net's group
    s("Obj2 := Obj1.BoardIterator_Create;",
      "Obj2.AddFilter_ObjectSet(MkSet(eNetObject));",
      "Obj2.AddFilter_LayerSet(AllLayers);",
      "Obj2.AddFilter_Method(eProcessAll);",
      "Obj4 := Obj2.FirstPCBObject;",
      "while Obj4 <> nil do")
    s.block()
    s("Obj5 := Obj4.GroupIterator_Create;",
      f"Obj5.AddFilter_ObjectSet(MkSet({KINDS}));",
      "Obj3 := Obj5.FirstPCBObject;",
      "while Obj3 <> nil do")
    s.block()
    s("List1.Add(IntToStr(Obj3.I_ObjectAddress) + '=' + Obj4.Name);")
    s("if Obj3.Net = nil then S2 := '' else S2 := Obj3.Net.Name;",
      "if S2 <> Obj4.Name then")
    s.block()
    s("I2 := I2 + 1;")
    _finding(s, "stale member", "Obj4.Name", limit)
    s.close()
    s("Obj3 := Obj5.NextPCBObject;")
    s.close()
    s("Obj4.GroupIterator_Destroy(Obj5);")
    if fix:
        # drop stale members one at a time: never change a group mid-walk.
        # I1 caps the tries per net, so a removal that does not take cannot
        # loop for ever (a 'g' in the fix log says the cap was reached)
        s("I3 := 1;", "I1 := 0;", "while (I3 = 1) and (I1 < 1000) do")
        s.block()
        s("I3 := 0;",
          "Obj5 := Obj4.GroupIterator_Create;",
          f"Obj5.AddFilter_ObjectSet(MkSet({KINDS}));",
          "Obj3 := Obj5.FirstPCBObject;",
          "while (Obj3 <> nil) and (I3 = 0) do")
        s.block()
        s("if Obj3.Net = nil then S2 := '' else S2 := Obj3.Net.Name;",
          "if S2 <> Obj4.Name then I3 := 1 else Obj3 := Obj5.NextPCBObject;")
        s.close()
        s("Obj4.GroupIterator_Destroy(Obj5);",
          "if I3 = 1 then")
        s.block()
        s("Obj4.RemovePCBObject(Obj3);",
          "S3 := S3 + 'r';",
          "I1 := I1 + 1;")
        s.close()
        s.close()
        s("if I1 >= 1000 then S3 := S3 + 'g';",
          "I3 := 0;")
    s("Obj4 := Obj2.NextPCBObject;")
    s.close()
    s("Obj1.BoardIterator_Destroy(Obj2);",
      "List1.Sorted := True;",
      "I1 := 0;")
    # pass 2: every primitive with a net
    s("Obj2 := Obj1.BoardIterator_Create;",
      f"Obj2.AddFilter_ObjectSet(MkSet({KINDS}));",
      "Obj2.AddFilter_LayerSet(AllLayers);",
      "Obj2.AddFilter_Method(eProcessAll);",
      "Obj3 := Obj2.FirstPCBObject;",
      "while Obj3 <> nil do")
    s.block()
    s("I1 := I1 + 1;",
      "if Obj3.Net <> nil then")
    s.block()
    s("if List1.IndexOf(IntToStr(Obj3.I_ObjectAddress) + '=' + Obj3.Net.Name) < 0 then")
    s.block()
    s("I3 := I3 + 1;")
    _finding(s, "unregistered", "''", limit)
    if fix:
        s("Obj3.BeginModify;",
          "Obj3.Net.AddPCBObject(Obj3);",
          "Obj3.EndModify;",
          "S3 := S3 + 'a';")
    s.close()
    s.close()
    s("Obj3 := Obj2.NextPCBObject;")
    s.close()
    s("Obj1.BoardIterator_Destroy(Obj2);",
      "List1.Free;")
    if fix:
        s("PCBServer.PostProcess;")
    s("ResultText := '{\"checked\": ' + IntToStr(I1) + ', \"stale_members\": ' + IntToStr(I2) + "
      "', \"unregistered\": ' + IntToStr(I3) + ', \"fixed\": ' + IntToStr(Length(S3)) + "
      "', \"fix_log\": \"' + S3 + '\", \"listed\": ' + IntToStr(B1) + "
      "', \"findings\": [' + S1 + ']}';")
    s.close()
    body = s.text()
    import script_preflight as sp
    rep = sp.check(body, SANDBOX)
    if not rep["ok"]:
        raise LiveError("membership audit script fails preflight: " + sp.format_report(rep))
    return body


def cmd_membership_audit(args):
    body = membership_script(args.board_path, fix=args.fix, limit=args.limit)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(body)
        print(json.dumps({"script": args.out, "fix": args.fix, "proven": False,
                          "run": "run_altium_script with this body; the board must be open"}))
    else:
        print(body, end="")
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("--board-path", required=True, help="the board's path as Altium has it open")
        p.add_argument("--fix", action="store_true", help="also repair the groups")
        p.add_argument("--limit", type=int, default=200, help="findings to list")
        p.add_argument("-o", "--out", help="write the script here (default: stdout)")
    command("membership-audit", cmd_membership_audit,
            "live script: primitives whose Net disagrees with the net's member group", build)
