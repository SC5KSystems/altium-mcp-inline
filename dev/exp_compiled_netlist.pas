// READ-ONLY. Write Altium's compiled netlist of one open project to
// C:\Users\Public\altium_mcp\compiled_netlist.txt, one line per net:
//     NET|<net name>|<designator>.<pin>|<designator>.<pin>|...
// Compare it with the offline reader:
//     python -m offline sch-compare <project>.PrjPcb C:\Users\Public\altium_mcp\compiled_netlist.txt
// (or the MCP tool offline_sch_compare). On the project the reader was built against,
// all 71 nets and 327 pins matched.
//
// Set S3 to text that appears in the project's file name. The project is compiled first
// (DM_Compile), which changes no document.
//
// List1 is declared by the sandbox but NOT created: create and free it here, or the first
// call on it stops the script in the debugger.

S3 := 'MyProject.PRJPCB';

Obj1 := nil;
for I1 := 0 to GetWorkspace.DM_ProjectCount - 1 do
begin
    Obj2 := GetWorkspace.DM_Projects(I1);
    if Pos(UpperCase(S3), UpperCase(Obj2.DM_ProjectFileName)) > 0 then
        Obj1 := Obj2;
end;
if Obj1 = nil then
    ResultText := 'no open project matches ' + S3
else
begin
    SandboxLog('compiling ' + Obj1.DM_ProjectFullPath);
    Obj1.DM_Compile;
    Obj2 := Obj1.DM_DocumentFlattened;
    if Obj2 = nil then
        ResultText := 'no flattened document after compile'
    else
    begin
        SandboxLog('net count ' + IntToStr(Obj2.DM_NetCount));
        List1 := TStringList.Create;
        for I1 := 0 to Obj2.DM_NetCount - 1 do
        begin
            Obj3 := Obj2.DM_Nets(I1);
            S1 := 'NET|' + Obj3.DM_NetName;
            for I2 := 0 to Obj3.DM_PinCount - 1 do
            begin
                Obj4 := Obj3.DM_Pins(I2);
                S1 := S1 + '|' + Obj4.DM_PhysicalPartDesignator + '.' + Obj4.DM_PinNumber;
            end;
            List1.Add(S1);
        end;
        List1.SaveToFile('C:\Users\Public\altium_mcp\compiled_netlist.txt');
        ResultText := 'nets ' + IntToStr(Obj2.DM_NetCount) + ' written';
        List1.Free;
    end;
end;
