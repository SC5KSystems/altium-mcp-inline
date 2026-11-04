// Collect, destroy the iterator, then remove (proven pattern).
Obj1 := PCBServer.GetPCBBoardByPath('C:\scratch\scratch.PcbDoc');
Obj5 := TInterfaceList.Create;
Obj2 := Obj1.BoardIterator_Create;
Obj2.AddFilter_ObjectSet(MkSet(eTrackObject, eViaObject));
Obj2.AddFilter_LayerSet(AllLayers);
Obj2.AddFilter_Method(eProcessAll);
Obj3 := Obj2.FirstPCBObject;
while Obj3 <> nil do
begin
    if (Obj3.Net <> nil) and (not Obj3.InComponent) and (not Obj3.InPolygon) then
        if Obj3.Net.Name = 'SENSE_TMP' then
            Obj5.Add(Obj3);
    Obj3 := Obj2.NextPCBObject;
end;
Obj1.BoardIterator_Destroy(Obj2);
PCBServer.PreProcess;
for I1 := 0 to Obj5.Count - 1 do
    Obj1.RemovePCBObject(Obj5.Items[I1]);
PCBServer.PostProcess;
Client.GetDocumentByPath('C:\scratch\scratch.PcbDoc').DoFileSave('');
ResultText := '{"removed": ' + IntToStr(Obj5.Count) + '}';
