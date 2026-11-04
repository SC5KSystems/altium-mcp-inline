// Rename a net (proven 2026-11-02).
Obj1 := PCBServer.GetPCBBoardByPath('C:\scratch\scratch.PcbDoc');
if Obj1 = nil then
begin
    ResultText := '{"error": "board not open"}';
    Exit;
end;
Obj2 := Obj1.BoardIterator_Create;
Obj2.AddFilter_ObjectSet(MkSet(eNetObject));
Obj2.AddFilter_LayerSet(AllLayers);
Obj2.AddFilter_Method(eProcessAll);
Obj3 := Obj2.FirstPCBObject;
I1 := 0;
while Obj3 <> nil do
begin
    if Obj3.Name = 'VBAT_OLD' then
    begin
        PCBServer.SendMessageToRobots(Obj3.I_ObjectAddress, c_Broadcast, PCBM_BeginModify, c_NoEventData);
        Obj3.Name := 'VBAT';
        PCBServer.SendMessageToRobots(Obj3.I_ObjectAddress, c_Broadcast, PCBM_EndModify, c_NoEventData);
        I1 := I1 + 1;
    end;
    Obj3 := Obj2.NextPCBObject;
end;
Obj1.BoardIterator_Destroy(Obj2);
ResultText := '{"renamed": ' + IntToStr(I1) + '}';
