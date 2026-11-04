// Move a pad to another net and register it in the net's member group.
Obj1 := PCBServer.GetPCBBoardByPath('C:\scratch\scratch.PcbDoc');
Obj2 := Obj1.BoardIterator_Create;
Obj2.AddFilter_ObjectSet(MkSet(eNetObject));
Obj2.AddFilter_LayerSet(AllLayers);
Obj2.AddFilter_Method(eProcessAll);
Obj4 := nil;
Obj3 := Obj2.FirstPCBObject;
while Obj3 <> nil do
begin
    if Obj3.Name = 'VBAT' then Obj4 := Obj3;
    Obj3 := Obj2.NextPCBObject;
end;
Obj1.BoardIterator_Destroy(Obj2);
SandboxLog('target net found: ' + BoolToStr(Obj4 <> nil, True));
Obj2 := Obj1.BoardIterator_Create;
Obj2.AddFilter_ObjectSet(MkSet(ePadObject));
Obj2.AddFilter_LayerSet(AllLayers);
Obj2.AddFilter_Method(eProcessAll);
Obj3 := Obj2.FirstPCBObject;
while Obj3 <> nil do
begin
    if Obj3.InComponent then
        if (Obj3.Component.Name.Text = 'R7') and (Obj3.Name = '2') then
        begin
            PCBServer.SendMessageToRobots(Obj3.I_ObjectAddress, c_Broadcast, PCBM_BeginModify, c_NoEventData);
            Obj3.Net := Obj4;
            Obj4.AddPCBObject(Obj3);
            PCBServer.SendMessageToRobots(Obj3.I_ObjectAddress, c_Broadcast, PCBM_EndModify, c_NoEventData);
        end;
    Obj3 := Obj2.NextPCBObject;
end;
Obj1.BoardIterator_Destroy(Obj2);
ResultText := '{"ok": true}';
